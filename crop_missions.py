#!/usr/bin/env python3
"""
crop_missions.py — crop all cameras in a mission to a time window given on the
reference camera's timeline, keeping every camera synchronized.

You give a start/end time relative to the REFERENCE camera's video. Each other
camera is cropped to the same real-world window using its measured clock offset:
gyro_offsets_s (preferred, sub-frame accurate) or sync_offsets_s (creation-time
fallback) from data/metadata.json.

Behavior (mirrors compact_missions.py):
  - Each camera's main video is cropped to {camera}.MP4
  - The pre-crop original is moved into the raw/ subfolder
  - With --lrv, the LRV proxy for each camera is cropped to {camera}_LRV.MP4 too

Offset convention (same as sync_gyro.py / metadata.json):
  offset[C] = seconds camera C started AFTER the reference camera.
  An event at reference time t_ref is at camera time t_C = t_ref - offset[C].

Safe by default: dry-run only. Pass --execute to actually crop.

Usage:
  python crop_missions.py /path/to/Mission --start 200 --end 400
  python crop_missions.py /path/to/Mission --start 3:20 --end 6:40 --lrv
  python crop_missions.py /path/to/Mission --start 200 --end 400 --execute
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from utils import MISSION_MARKER, ffprobe

RAW_DIR = "raw"
CROP_FILE = "crop.yaml"


# ── time parsing ─────────────────────────────────────────────────────────────

def parse_time(s: str) -> float:
    """Parse seconds ('123.5'), MM:SS ('3:20'), or HH:MM:SS ('1:03:20') → float seconds."""
    s = s.strip()
    if ":" in s:
        parts = s.split(":")
        if len(parts) > 3:
            raise ValueError(f"invalid time: {s}")
        parts = [float(p) for p in parts]
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec
    return float(s)


def fmt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    if h:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"


# ── mission discovery ────────────────────────────────────────────────────────

def find_missions(root: Path) -> list[Path]:
    if (root / MISSION_MARKER).exists() or (root / "data" / "metadata.json").exists():
        return [root]
    missions = sorted(p.parent for p in root.glob(f"*/{MISSION_MARKER}"))
    if not missions:
        sys.exit(
            f"error: no missions found under {root}\n"
            f"  (no {MISSION_MARKER} markers in immediate subfolders)"
        )
    return missions


# ── metadata / offsets ───────────────────────────────────────────────────────

def load_metadata(mission: Path) -> dict | None:
    path = mission / "data" / "metadata.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def resolve_offsets(meta: dict) -> tuple[str, dict[str, tuple[float, str]]]:
    """
    Return (reference_camera, {camera: (offset_s, source)}).
    Per camera, prefer gyro_offsets_s, else sync_offsets_s.
    """
    gyro = meta.get("gyro_offsets_s", {}) or {}
    sync = meta.get("sync_offsets_s", {}) or {}
    cameras = list(meta.get("cameras", {}).keys()) or sorted(set(gyro) | set(sync))

    # Reference: camera with 0.0 offset (prefer gyro's reference, else sync's)
    ref = next((c for c, v in gyro.items() if v == 0.0), None)
    if ref is None:
        ref = next((c for c, v in sync.items() if v == 0.0), None)

    offsets: dict[str, tuple[float, str]] = {}
    for cam in cameras:
        g = gyro.get(cam)
        if g is not None:
            offsets[cam] = (float(g), "gyro")
            continue
        s = sync.get(cam)
        if s is not None:
            offsets[cam] = (float(s), "sync")
            continue
        offsets[cam] = (None, "missing")
    return ref, offsets


# ── video discovery ──────────────────────────────────────────────────────────

def find_main_video(mission: Path, camera: str) -> Path | None:
    """Resolve the camera's primary MP4: compacted {camera}.MP4 or single-chapter GX file."""
    candidates = [mission / f"{camera}.MP4"]
    candidates += sorted(mission.glob(f"GX*_{camera}.MP4"))
    return next((p for p in candidates if p.exists()), None)


def find_lrv_video(mission: Path, camera: str) -> Path | None:
    """Resolve the camera's LRV proxy in any of its post-compact forms."""
    candidates = [mission / f"{camera}_LRV.MP4"]
    candidates += sorted(mission.glob(f"GL*_{camera}_LRV.MP4"))
    candidates += sorted(mission.glob(f"GL*_{camera}.LRV"))
    return next((p for p in candidates if p.exists()), None)


# ── ffmpeg crop ──────────────────────────────────────────────────────────────

def ffmpeg_crop(src: Path, dst: Path, start: float, dur: float,
                dry_run: bool, reencode: bool) -> bool:
    if dry_run:
        return True

    # Map video + audio only. The GoPro timecode (tmcd) and GPMF (gpmd) data
    # streams can't be remuxed into a fresh MP4 here and aren't needed in the
    # cropped viewing clips (telemetry already lives in data/ CSVs).
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
           "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
           "-map", "0:v:0", "-map", "0:a?"]
    if reencode:
        # frame-accurate: re-encode video, copy audio
        cmd += ["-c:v", "libx265", "-crf", "18", "-c:a", "copy"]
    else:
        # fast: stream copy; cut lands on nearest keyframe at/before start
        cmd += ["-c", "copy"]
    cmd += ["-y", str(dst)]

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"    ERROR: ffmpeg failed (exit {result.returncode})", file=sys.stderr)
        return False
    if not dst.exists() or dst.stat().st_size == 0:
        print(f"    ERROR: output missing or empty: {dst}", file=sys.stderr)
        return False
    size_mb = dst.stat().st_size / 1_000_000
    print(f"    -> {dst.name}  ({size_mb:.0f} MB)")
    return True


# ── per-mission processing ───────────────────────────────────────────────────

def _window_for(start_ref: float, end_ref: float, offset: float,
                src_dur: float | None) -> tuple[float, float, list[str]]:
    """Compute (start, dur) on a camera's timeline, clamped to its footage."""
    s = start_ref - offset
    e = end_ref - offset
    warns: list[str] = []

    if s < 0:
        warns.append(f"window starts {(-s):.2f}s before footage — clamped (clip aligns at END)")
        s = 0.0
    if src_dur is not None:
        if s > src_dur:
            warns.append(f"window is entirely after footage ends ({src_dur:.1f}s) — nothing to crop")
            return s, 0.0, warns
        if e > src_dur:
            warns.append(f"window ends {(e - src_dur):.2f}s past footage — clamped (clip aligns at START)")
            e = src_dur
    return s, max(0.0, e - s), warns


def write_crop_record(mission: Path, ref: str, start_ref: float, end_ref: float,
                      method: str, lrv: bool, records: list[dict]) -> None:
    """Write crop.yaml documenting the crop applied to this mission."""
    doc = {
        "reference_camera": ref,
        "window": {
            "start_s": round(start_ref, 3),
            "end_s": round(end_ref, 3),
            "duration_s": round(end_ref - start_ref, 3),
            "start_hms": fmt_time(start_ref),
            "end_hms": fmt_time(end_ref),
        },
        "method": method,
        "lrv": lrv,
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": ("Originals are preserved in raw/. Re-run crop_missions.py with a new "
                 "--start/--end to re-cut from those originals; this file is rewritten."),
        "crops": records,
    }
    with open(mission / CROP_FILE, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)


def crop_mission(mission: Path, start_ref: float, end_ref: float,
                 lrv: bool, dry_run: bool, force: bool, reencode: bool) -> bool:
    meta = load_metadata(mission)
    if meta is None:
        print("  No data/metadata.json — skipping (run extract_telemetry.py + sync_gyro.py first)")
        return True
    ref, offsets = resolve_offsets(meta)
    if ref is None:
        print("  No reference camera (offset 0.0) in metadata.json — skipping")
        return True
    cameras = sorted(offsets)

    raw_dir = mission / RAW_DIR

    # If this mission was already cropped, re-cut from the recorded originals
    # in raw/ rather than the (already-cropped) files in the mission folder.
    crop_path = mission / CROP_FILE
    recrop = crop_path.exists()
    prev_originals: dict[str, str] = {}   # output name -> original path (relative to mission)
    if recrop:
        try:
            prev = yaml.safe_load(crop_path.read_text()) or {}
            prev_originals = {c.get("output"): c.get("original")
                              for c in prev.get("crops", [])}
        except Exception:
            prev_originals = {}

    print(f"  Reference camera: {ref}")
    print(f"  Window on reference: {fmt_time(start_ref)} – {fmt_time(end_ref)}  "
          f"({end_ref - start_ref:.2f}s)")
    if recrop:
        print(f"  {CROP_FILE} found — re-cropping from originals in raw/")

    # Build job list. Each job: dict with all info for cropping + recording.
    jobs: list[dict] = []
    pre_flight_ok = True

    for cam in cameras:
        offset, off_src = offsets[cam]
        if offset is None:
            print(f"  {cam}: no offset in metadata — skipping")
            continue

        for kind, finder, out_name in (
            ("MP4", find_main_video, f"{cam}.MP4"),
            ("LRV", find_lrv_video,  f"{cam}_LRV.MP4"),
        ):
            if kind == "LRV" and not lrv:
                continue

            # Resolve the source. If this output was cropped before, re-cut from
            # the recorded original (in raw/). Otherwise it's a first crop for
            # this file: take the original from the mission folder and move it.
            if out_name in prev_originals:
                src = mission / prev_originals[out_name]
                needs_move = False
                if not src.exists():
                    print(f"  {cam} {kind}: recorded original {prev_originals[out_name]} "
                          f"missing — skipping (won't re-crop the cropped {out_name})")
                    continue
            else:
                src = finder(mission, cam)
                needs_move = True
                if src is None:
                    if kind == "MP4":
                        print(f"  {cam}: no source video found — skipping")
                    continue

            _, src_dur = ffprobe(str(src))
            s, dur, warns = _window_for(start_ref, end_ref, offset, src_dur)
            dst = mission / out_name

            tag = "REF" if cam == ref else f"{offset:+.3f}s [{off_src}]"
            print(f"  {cam} {kind}: {src.name}  offset={tag}")
            print(f"      crop {fmt_time(s)} +{dur:.2f}s -> {out_name}")
            for w in warns:
                print(f"      ! {w}")

            if dur <= 0:
                print(f"      (empty window — skipping)")
                continue

            # A to-be-moved original must not collide with something in raw/.
            raw_dst = raw_dir / src.name
            if needs_move and raw_dst.exists() and not force:
                print(f"      ! raw/{src.name} already exists — use --force")
                pre_flight_ok = False

            jobs.append({
                "cam": cam, "kind": kind, "src": src, "dst": dst,
                "start": s, "dur": dur, "raw_dst": raw_dst, "needs_move": needs_move,
                "offset": offset, "off_src": off_src, "out_name": out_name,
            })

    if not jobs:
        print("  Nothing to crop.")
        return True

    if not pre_flight_ok:
        print("  Pre-flight failed — aborting mission.", file=sys.stderr)
        return False

    # The crop consumes the reference offset, so an overlay built for the full
    # video must be regenerated with --crop-offset = the reference start time.
    if (mission / "overlays.yaml").exists():
        print(f"\n  Regenerate the overlay aligned to the cropped clip:")
        print(f"    python overlay_stats.py {mission} --crop-offset {start_ref:g} --force")

    method = "reencode" if reencode else "stream-copy"

    if dry_run:
        moved = sum(1 for j in jobs if j["needs_move"])
        print(f"\n  [dry-run] would crop {len(jobs)} file(s), "
              f"move {moved} original(s) to raw/, write {CROP_FILE}")
        return True

    raw_dir.mkdir(exist_ok=True)
    if not (raw_dir / MISSION_MARKER).exists():
        open(raw_dir / MISSION_MARKER, "w").close()

    any_error = False
    records: list[dict] = []
    for job in jobs:
        dst = job["dst"]
        tmp = dst.with_name(f"{dst.stem}.cropping{dst.suffix}")
        print(f"  {job['cam']} {job['kind']}: cropping ...")
        if not ffmpeg_crop(job["src"], tmp, job["start"], job["dur"],
                           dry_run=False, reencode=reencode):
            any_error = True
            if tmp.exists():
                tmp.unlink()
            continue

        if job["needs_move"]:
            # Move pre-crop original into raw/, then put cropped file in place.
            shutil.move(str(job["src"]), str(job["raw_dst"]))
            original = job["raw_dst"]
            print(f"      original -> raw/{job['raw_dst'].name}")
        else:
            # Original already in raw/; just replace the cropped output.
            original = job["src"]
        os.replace(tmp, dst)

        records.append({
            "camera": job["cam"],
            "kind": job["kind"],
            "output": job["out_name"],
            "original": str(original.relative_to(mission)),
            "offset_s": round(job["offset"], 6),
            "offset_src": "ref" if job["cam"] == ref else job["off_src"],
            "crop_start_s": round(job["start"], 3),
            "crop_dur_s": round(job["dur"], 3),
        })

    if any_error:
        print("  Some crops failed — see errors above.", file=sys.stderr)
        return False

    write_crop_record(mission, ref, start_ref, end_ref, method, lrv, records)
    print(f"  Wrote {CROP_FILE}")
    return True


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("mission", type=Path,
                    help="mission folder (or parent folder of missions)")
    ap.add_argument("--start", required=True, type=parse_time, metavar="T",
                    help="window start on reference timeline (sec, MM:SS, or HH:MM:SS)")
    ap.add_argument("--end", required=True, type=parse_time, metavar="T",
                    help="window end on reference timeline")
    ap.add_argument("--lrv", action="store_true",
                    help="also crop LRV proxy videos to {camera}_LRV.MP4")
    ap.add_argument("--reencode", action="store_true",
                    help="frame-accurate crop via re-encode (slow; default is fast keyframe-aligned stream copy)")
    ap.add_argument("--execute", action="store_true",
                    help="actually perform the crop (default: dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite if a pre-crop original already exists in raw/")
    args = ap.parse_args()

    if args.end <= args.start:
        sys.exit(f"error: --end ({args.end}) must be greater than --start ({args.start})")

    if not args.mission.exists():
        sys.exit(f"error: not found: {args.mission}")

    missions = find_missions(args.mission)
    dry_run = not args.execute

    print(f"Mode:  {'DRY RUN' if dry_run else 'EXECUTE'}"
          f"{'  | --lrv' if args.lrv else ''}"
          f"{'  | --reencode' if args.reencode else '  | stream-copy'}")
    print(f"Found: {len(missions)} mission(s)")
    if dry_run:
        print("DRY RUN — no files will be modified. Re-run with --execute to apply.")
    if not args.reencode:
        print("Note: stream-copy cuts land on the nearest keyframe at/before each start "
              "(sub-second). Use --reencode for frame-accurate cuts.")

    ok = err = 0
    for mission in missions:
        print(f"\n[{mission.name}]")
        if crop_mission(mission, args.start, args.end,
                        args.lrv, dry_run, args.force, args.reencode):
            ok += 1
        else:
            err += 1

    print(f"\nDone. {ok} ok, {err} failed.")
    if err:
        sys.exit(1)


if __name__ == "__main__":
    main()
