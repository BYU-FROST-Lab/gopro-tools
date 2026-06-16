#!/usr/bin/env python3
"""
play_missions.py — play every camera in a mission at the same time with mpv.

Launches one mpv window per camera, tiled across the screen, all seeked to the
same real-world moment. Start time is given on the REFERENCE camera's timeline;
each other camera is offset using its measured clock offset from
data/metadata.json (gyro_offsets_s preferred, sync_offsets_s fallback) so the
windows stay in sync — the same convention as crop_missions.py.

If there is no metadata.json, every camera just starts at --start with no offset.

Audio: only the reference camera is unmuted by default (so you don't get N
overlapping soundtracks). Use --no-mute to hear them all.

Usage:
  python play_missions.py /path/to/Mission
  python play_missions.py /path/to/Mission --start 3:20 --speed 0.5
  python play_missions.py /path/to/Mission --lrv --start 200
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from crop_missions import (
    find_lrv_video,
    find_main_video,
    find_missions,
    load_metadata,
    parse_time,
    resolve_offsets,
)


# ── window tiling ────────────────────────────────────────────────────────────

def grid_geometry(n: int) -> list[str]:
    """Return one mpv --geometry value per window, tiling an n-window grid.

    Uses percentage sizes/positions so it adapts to any screen resolution.
    """
    if n <= 0:
        return []
    cols = 1
    while cols * cols < n:
        cols += 1
    rows = (n + cols - 1) // cols
    w = 100 // cols
    h = 100 // rows
    geoms = []
    for i in range(n):
        r, c = divmod(i, cols)
        # mpv accepts percentage offsets: WxH+X+Y
        geoms.append(f"{w}%x{h}%+{c * w}%+{r * h}%")
    return geoms


# ── per-mission playback ─────────────────────────────────────────────────────

def find_subs(mission: Path) -> dict[str, Path]:
    """Map camera -> overlay subtitle file ({camera}_stats.ass from overlay_stats.py)."""
    subs: dict[str, Path] = {}
    for ass in sorted((mission / "overlays").glob("*_stats.ass")):
        cam = ass.name[: -len("_stats.ass")]
        subs[cam] = ass
    return subs


def play_mission(mission: Path, start_ref: float, lrv: bool, speed: float,
                 sync: bool, mute: bool, tile: bool, subs_mode: str,
                 dry_run: bool) -> bool:
    meta = load_metadata(mission)

    if meta is not None and sync:
        ref, offsets = resolve_offsets(meta)
    else:
        ref, offsets = None, {}

    # Resolve which cameras / videos to play.
    finder = find_lrv_video if lrv else find_main_video
    if offsets:
        cameras = sorted(offsets)
    else:
        # No metadata (or --no-sync): discover cameras from the files on disk.
        cameras = sorted({_camera_of(p) for p in _all_videos(mission, lrv)})

    jobs: list[tuple[str, Path, float]] = []  # (camera, path, start_s)
    for cam in cameras:
        src = finder(mission, cam)
        if src is None:
            print(f"  {cam}: no {'LRV' if lrv else 'MP4'} video found — skipping")
            continue

        offset = 0.0
        if cam in offsets:
            off, _src = offsets[cam]
            offset = 0.0 if off is None else off
        start_c = max(0.0, start_ref - offset)
        jobs.append((cam, src, start_c))

    if not jobs:
        print("  Nothing to play.")
        return True

    geoms = grid_geometry(len(jobs)) if tile else [None] * len(jobs)

    # Which window keeps its audio: the reference camera, or the first window
    # when there's no reference (so --no-sync mode isn't completely silent).
    audio_cam = ref if ref in {c for c, _, _ in jobs} else jobs[0][0]

    # Overlay subtitles: "none" off, "all" wherever a _stats.ass exists, else
    # only on the named camera.
    subs = {} if subs_mode == "none" else find_subs(mission)
    if subs_mode not in ("none", "all"):
        subs = {c: p for c, p in subs.items() if c == subs_mode}
        if not subs:
            print(f"  ! no overlay subtitle for camera '{subs_mode}' "
                  f"(have: {', '.join(find_subs(mission)) or 'none'})")

    procs = []
    for (cam, src, start_c), geom in zip(jobs, geoms):
        is_ref = (cam == ref)
        tag = "REF" if is_ref else (f"start {start_c:.2f}s" if start_c else "start 0s")
        print(f"  {cam}: {src.name}  ({tag})")

        cmd = ["mpv", f"--start={start_c:.3f}", f"--speed={speed:g}",
               f"--title={mission.name}/{cam}"]
        if geom:
            cmd.append(f"--geometry={geom}")
        if mute and cam != audio_cam:
            cmd.append("--mute=yes")
        if cam in subs:
            cmd.append(f"--sub-file={subs[cam]}")
            print(f"      + subtitle: overlays/{subs[cam].name}")
        cmd.append(str(src))

        if dry_run:
            print(f"      {' '.join(cmd)}")
            continue
        procs.append(subprocess.Popen(cmd))

    if dry_run:
        return True

    print(f"\n  Launched {len(procs)} mpv window(s). Close them or Ctrl-C here to stop.")
    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        print("\n  Stopping all windows...")
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait()
    return True


def _all_videos(mission: Path, lrv: bool) -> list[Path]:
    if lrv:
        return (list(mission.glob("*_LRV.MP4")) + list(mission.glob("GL*.LRV")))
    vids = [p for p in mission.glob("*.MP4") if not p.name.endswith("_LRV.MP4")]
    return vids


def _camera_of(path: Path) -> str:
    """Best-effort camera name from a compacted/organized filename."""
    name = path.name
    for suffix in ("_LRV.MP4", ".MP4", ".LRV"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # organized chapter form: GX0147_Front -> Front ; GL..._Front -> Front
    if "_" in name:
        return name.split("_", 1)[1]
    return name


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("mission", type=Path,
                    help="mission folder (or parent folder of missions)")
    ap.add_argument("--start", type=parse_time, default=0.0, metavar="T",
                    help="start time on the reference timeline (sec, MM:SS, or HH:MM:SS)")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="playback speed multiplier (default 1.0)")
    ap.add_argument("--lrv", action="store_true",
                    help="play LRV proxies instead of full-res MP4s")
    ap.add_argument("--no-sync", dest="sync", action="store_false",
                    help="ignore metadata offsets; start every camera at --start")
    ap.add_argument("--no-mute", dest="mute", action="store_false",
                    help="play audio from every window (default: only reference)")
    ap.add_argument("--no-tile", dest="tile", action="store_false",
                    help="don't auto-tile windows into a grid")
    ap.add_argument("--subs", default="all", metavar="MODE",
                    help="overlay subtitles: 'all' (default, every camera that has a "
                         "_stats.ass), 'none', or a camera name to show them on only "
                         "that one")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the mpv commands without launching")
    args = ap.parse_args()

    if not args.mission.exists():
        sys.exit(f"error: not found: {args.mission}")
    if shutil.which("mpv") is None and not args.dry_run:
        sys.exit("error: mpv not found on PATH (install mpv, or use --dry-run)")

    missions = find_missions(args.mission)
    if len(missions) > 1:
        sys.exit(
            f"error: {args.mission} contains {len(missions)} missions — "
            f"point at a single mission folder to play it."
        )

    print(f"Source: {'LRV proxies' if args.lrv else 'full-res MP4'}  | "
          f"speed {args.speed:g}x  | "
          f"{'synced' if args.sync else 'no-sync'}")
    mission = missions[0]
    print(f"\n[{mission.name}]")
    play_mission(mission, args.start, args.lrv, args.speed,
                 args.sync, args.mute, args.tile, args.subs, args.dry_run)


if __name__ == "__main__":
    main()
