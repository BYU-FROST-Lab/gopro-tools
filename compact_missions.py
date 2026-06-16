#!/usr/bin/env python3
"""
compact_missions.py

Concatenate GoPro chapter files within organized mission folders into single
videos per camera, archiving originals to a raw/ subfolder.

Default behavior:
  - MP4 chapters: concat if multi-chapter; single-chapter left with its GX filename
  - LRV proxy files: always moved to raw/ (LRVs are already compressed)
  - THM thumbnail files: always moved to raw/

With --lrv:
  - LRV chapters: concat if multi-chapter; single-chapter left with its GL filename
  - THM thumbnail files: still always moved to raw/

Scans root directory for subfolders marked with .gopro_mission (written by
organize_missions.py --execute). Safe by default: dry-run only.

Usage:
    python compact_missions.py /path/to/footage              # dry-run
    python compact_missions.py /path/to/footage --execute     # run it
    python compact_missions.py /path/to/footage --execute --lrv  # also compile LRV proxies
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict

from utils import (MISSION_MARKER, VIDEO_EXTS, PROXY_EXTS, THUMB_EXTS,
                   COMPACT_RE, find_missions, mission_compacted)

RAW_DIR = "raw"


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def parse_mission_files(folder):
    """Parse organized GoPro files in a mission folder.
    Returns dict: (camera, ext_upper) -> [(chapter_int, path), ...] sorted by chapter."""
    by_cam_ext = defaultdict(list)
    try:
        entries = os.listdir(folder)
    except OSError as e:
        print(f"  Warning: cannot list {folder}: {e}", file=sys.stderr)
        return {}
    for fn in entries:
        m = COMPACT_RE.match(fn)
        if not m:
            continue
        by_cam_ext[(m.group("camera"), m.group("ext").upper())].append(
            (int(m.group("chapter")), os.path.join(folder, fn))
        )
    for key in by_cam_ext:
        by_cam_ext[key].sort()
    return dict(by_cam_ext)


def ffmpeg_concat(chapter_paths, output_path, dry_run, output_fmt=None):
    """Concatenate chapter_paths into output_path using stream copy.
    output_fmt: override the output container format (e.g. 'mp4' for .LRV files).
    Returns True on success."""
    if dry_run:
        print(f"    [dry-run] concat {len(chapter_paths)} file(s) -> {os.path.basename(output_path)}")
        return True

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        list_file = f.name
        for p in chapter_paths:
            f.write(f"file '{p}'\n")
    try:
        # Concat to a sibling temp, validate, then os.replace into place — so a
        # killed/failed ffmpeg never leaves a truncated {camera}.MP4 behind.
        stem, ext = os.path.splitext(output_path)
        tmp_out = f"{stem}.concat.tmp{ext}"
        # -copy_unknown: copy streams ffmpeg can't identify (e.g. GoPro GPMF telemetry/GPS)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-f", "concat", "-safe", "0", "-i", list_file,
               "-c", "copy", "-copy_unknown"]
        if output_fmt:
            cmd += ["-f", output_fmt]
        cmd += ["-y", tmp_out]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"    ERROR: ffmpeg failed (exit {result.returncode})", file=sys.stderr)
            _safe_unlink(tmp_out)
            return False
        if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
            print(f"    ERROR: output missing or empty: {output_path}", file=sys.stderr)
            _safe_unlink(tmp_out)
            return False
        os.replace(tmp_out, output_path)
        size_mb = os.path.getsize(output_path) / 1_000_000
        print(f"    -> {os.path.basename(output_path)}  ({size_mb:.0f} MB)")
        return True
    finally:
        try:
            os.unlink(list_file)
        except OSError:
            pass


def compact_mission(folder, lrv, dry_run, force):
    """Process one mission folder. Returns True on success or skip, False on error."""
    raw_dir = os.path.join(folder, RAW_DIR)

    # Completion is signalled by the marker written into raw/ as the LAST step.
    if mission_compacted(folder):
        print(f"  Already compacted (raw/ marker present) — skipping.")
        return True
    if os.path.exists(raw_dir) and not force:
        # raw/ exists but no completion marker: a prior run was interrupted.
        # Don't silently skip — surface it rather than treat partial as done.
        print(f"  raw/ exists without completion marker — looks partially compacted.\n"
              f"  Inspect {raw_dir} and re-run with --force to finish.", file=sys.stderr)
        return False

    files = parse_mission_files(folder)
    if not files:
        print(f"  No GoPro files found — skipping.")
        return True

    mp4_cameras = sorted({cam for cam, ext in files if ext in VIDEO_EXTS})
    lrv_cameras = sorted({cam for cam, ext in files if ext in PROXY_EXTS})

    multi_mp4 = [c for c in mp4_cameras if len(files.get((c, "MP4"), [])) > 1]
    single_mp4 = [c for c in mp4_cameras if len(files.get((c, "MP4"), [])) == 1]
    multi_lrv  = [c for c in lrv_cameras if len(files.get((c, "LRV"), [])) > 1]
    single_lrv = [c for c in lrv_cameras if len(files.get((c, "LRV"), [])) == 1]

    # Pre-flight: check output collisions
    if not force:
        for cam in multi_mp4:
            out = os.path.join(folder, f"{cam}.MP4")
            if os.path.exists(out):
                print(f"  Output {cam}.MP4 already exists. Use --force to overwrite.")
                return False
        if lrv:
            for cam in single_lrv:
                src = files[(cam, "LRV")][0][1]
                stem, _ = os.path.splitext(os.path.basename(src))
                dst = os.path.join(folder, f"{stem}_LRV.MP4")
                if os.path.exists(dst):
                    print(f"  Output {os.path.basename(dst)} already exists. Use --force to overwrite.")
                    return False
            for cam in multi_lrv:
                out = os.path.join(folder, f"{cam}_LRV.MP4")
                if os.path.exists(out):
                    print(f"  Output {cam}_LRV.MP4 already exists. Use --force to overwrite.")
                    return False

    any_error = False

    # --- MP4 ---
    for cam in single_mp4:
        fn = os.path.basename(files[(cam, "MP4")][0][1])
        print(f"  {cam} MP4: 1 chapter — leaving {fn} as-is")

    for cam in multi_mp4:
        paths = [p for _, p in files[(cam, "MP4")]]
        print(f"  {cam} MP4: {len(paths)} chapters -> {cam}.MP4")
        if not ffmpeg_concat(paths, os.path.join(folder, f"{cam}.MP4"), dry_run):
            any_error = True

    # --- LRV ---
    if lrv:
        for cam in single_lrv:
            src = files[(cam, "LRV")][0][1]
            stem, _ = os.path.splitext(os.path.basename(src))
            dst = os.path.join(folder, f"{stem}_LRV.MP4")
            print(f"  {cam} LRV: 1 chapter — renaming to {os.path.basename(dst)}")
            if not dry_run:
                os.rename(src, dst)
        for cam in multi_lrv:
            paths = [p for _, p in files[(cam, "LRV")]]
            print(f"  {cam} LRV: {len(paths)} chapters -> {cam}_LRV.MP4")
            if not ffmpeg_concat(paths, os.path.join(folder, f"{cam}_LRV.MP4"), dry_run):
                any_error = True
    else:
        lrv_count = sum(len(v) for (_, ext), v in files.items() if ext in PROXY_EXTS)
        if lrv_count:
            print(f"  LRV ({lrv_count} file(s)) -> raw/  (use --lrv to compile instead)")

    if any_error:
        print(f"  Errors during concat — originals NOT moved.", file=sys.stderr)
        return False

    # --- Collect files to move to raw/ ---
    to_raw = []

    # Multi-chapter MP4 originals (single-chapter MP4s stay in place)
    for cam in multi_mp4:
        to_raw.extend(p for _, p in files.get((cam, "MP4"), []))

    # LRV: all go to raw/ unless --lrv, in which case only multi-chapter originals move
    if lrv:
        for cam in multi_lrv:
            to_raw.extend(p for _, p in files.get((cam, "LRV"), []))
    else:
        for (_, ext), chapter_list in files.items():
            if ext in PROXY_EXTS:
                to_raw.extend(p for _, p in chapter_list)

    # All THM files always go to raw/
    for (_, ext), chapter_list in files.items():
        if ext in THUMB_EXTS:
            to_raw.extend(p for _, p in chapter_list)

    if not to_raw:
        print(f"  Nothing to move to raw/.")
        return True

    if dry_run:
        print(f"  [dry-run] move {len(to_raw)} file(s) to raw/")
        print(f"  [dry-run] write {MISSION_MARKER} to raw/")
    else:
        os.makedirs(raw_dir, exist_ok=True)
        for src in to_raw:
            dst = os.path.join(raw_dir, os.path.basename(src))
            if os.path.exists(src):          # idempotent: a re-run may have already moved some
                shutil.move(src, dst)
        # Marker written LAST: its presence means the whole compact finished.
        open(os.path.join(raw_dir, MISSION_MARKER), "w").close()
        print(f"  Moved {len(to_raw)} file(s) to raw/")

    return True


def main():
    ap = argparse.ArgumentParser(
        description="Compact organized GoPro mission folders: concatenate chapters, archive originals to raw/."
    )
    ap.add_argument("root", help="Root folder containing organized mission subfolders")
    ap.add_argument("--execute", action="store_true",
                    help="Actually perform operations (default: dry-run)")
    ap.add_argument("--lrv", action="store_true",
                    help="Also compile multi-chapter LRV proxies (single-chapter LRVs left as-is)")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing output files")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit(f"Not a directory: {root}")

    missions = find_missions(root)
    if not missions:
        print(f"No mission folders found under {root}  (no {MISSION_MARKER} markers).")
        sys.exit(0)

    dry_run = not args.execute
    lrv_note = "  | --lrv: compile proxy chapters" if args.lrv else ""
    print(f"Root:  {root}")
    print(f"Mode:  {'DRY RUN' if dry_run else 'EXECUTE'}{lrv_note}")
    print(f"Found: {len(missions)} mission folder(s)")
    if dry_run:
        print("DRY RUN — no files will be modified. Re-run with --execute to apply.\n")

    ok_count = err_count = 0
    for folder in missions:
        print(f"\n[{os.path.basename(folder)}]")
        if compact_mission(folder, args.lrv, dry_run, args.force):
            ok_count += 1
        else:
            err_count += 1

    print(f"\nDone. {ok_count} succeeded, {err_count} failed.")
    if err_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
