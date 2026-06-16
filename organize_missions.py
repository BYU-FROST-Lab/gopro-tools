#!/usr/bin/env python3
"""
organize_missions.py

Organize multi-camera GoPro footage into mission folders, suffixing each file
with its camera mount. Groups chapters per camera by filename, then matches
recordings across cameras into missions using start time + duration.

SAFE BY DEFAULT: dry-run only. Nothing is moved until you pass --execute.
All filesystem operations are MOVES/RENAMES, never copies (files are large).

Usage:
    python organize_missions.py /path/to/footage              # dry-run
    python organize_missions.py /path/to/footage --timeline   # dry-run + sorted timeline
    python organize_missions.py /path/to/footage --execute     # actually do it

Layout expected:
    footage/
      left/   GX0101234.MP4  GL0101234.LRV  GX0101234.THM ...
      right/  ...
      front/  ...
  -> each subfolder name becomes the camera/mount suffix.

GoPro filename convention:
    GX + CC (2-digit chapter) + NNNN (4-digit video number) + .MP4
    GL + CC + NNNN + .LRV   (low-res proxy)
    GX + CC + NNNN + .THM   (thumbnail)
  Files sharing NNNN are chapters of ONE continuous recording.
"""

import argparse
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime

from utils import (
    MISSION_MARKER,
    NAME_RE, VIDEO_EXTS, PROXY_EXTS, THUMB_EXTS,
    FileEntry, Recording,
    ffprobe, thm_creation, mtime,
    best_start, total_duration,
)

# ---------------------------------------------------------------------------
# TUNABLE PARAMETERS  (override on the command line; see argparse below)
# ---------------------------------------------------------------------------
START_TOL_S   = 60.0   # max start-time gap (seconds) to call recordings the same mission
DUR_TOL_S     = 120.0  # max duration difference (seconds) to call recordings the same mission
WARN_TOL_FRAC = 0.6  # warn when a spread exceeds this fraction of the tolerance
SHORT_WARN_S  = 30.0   # warn if any recording in a mission is shorter than this (seconds)

# Folders that are outputs, not camera inputs (ignored when discovering cameras).
RESERVED_DIRS = {"other"}
MISSION_PREFIX = ""        # prefix prepended to every mission folder name; can be empty


# ---------------------------------------------------------------------------
# Discovery + grouping
# ---------------------------------------------------------------------------
def discover(root):
    """Walk camera subfolders. Returns (recordings, orphans)."""
    cameras = [
        d for d in sorted(os.listdir(root))
        if os.path.isdir(os.path.join(root, d))
        and d not in RESERVED_DIRS
        and not os.path.exists(os.path.join(root, d, MISSION_MARKER))
    ]

    recordings, orphans = [], []
    for cam in cameras:
        cam_dir = os.path.join(root, cam)
        by_video = defaultdict(list)
        for fn in sorted(os.listdir(cam_dir)):
            full = os.path.join(cam_dir, fn)
            if not os.path.isfile(full):
                continue
            m = NAME_RE.match(fn)
            if not m:
                orphans.append((full, cam, "unparseable name"))
                continue
            by_video[m.group("video")].append(FileEntry(
                path=full, camera=cam, prefix=m.group("prefix"),
                chapter=int(m.group("chapter")), video=m.group("video"),
                ext=m.group("ext").upper(),
            ))

        for video, files in by_video.items():
            has_mp4 = any(f.ext in VIDEO_EXTS for f in files)
            if not has_mp4:
                for f in files:
                    orphans.append((f.path, cam, f"no MP4 for video {video}"))
                continue
            rec = Recording(camera=cam, video=video, files=files)
            rec.start, rec.start_src = best_start(rec)
            rec.duration, rec.dur_src = total_duration(rec)
            recordings.append(rec)

    return recordings, orphans


def cluster_missions(recordings):
    """
    Anchor-based clustering (NOT chaining):
    sort by start; open a mission anchored to the first unassigned recording;
    admit later recordings whose start is within START_TOL of the anchor AND
    whose duration is within DUR_TOL of the anchor. Prevents drift.
    Recordings without a start time each become their own mission.
    """
    timed = sorted([r for r in recordings if r.start is not None], key=lambda r: r.start)
    untimed = [r for r in recordings if r.start is None]

    missions, used = [], [False] * len(timed)
    for i, anchor in enumerate(timed):
        if used[i]:
            continue
        group = [anchor]
        used[i] = True
        for j in range(i + 1, len(timed)):
            if used[j]:
                continue
            cand = timed[j]
            if cand.start - anchor.start > START_TOL_S:
                break  # sorted: nothing further can be in range
            if cand.camera in {r.camera for r in group}:
                continue  # one recording per camera per mission
            dur_ok = (
                anchor.duration is None or cand.duration is None
                or abs(cand.duration - anchor.duration) <= DUR_TOL_S
            )
            if dur_ok:
                group.append(cand)
                used[j] = True
        missions.append(group)

    for r in untimed:
        missions.append([r])

    missions.sort(key=lambda g: (g[0].start is None, g[0].start or 0))
    return missions


# ---------------------------------------------------------------------------
# Planning + reporting
# ---------------------------------------------------------------------------
def fmt_ts(epoch):
    if epoch is None:
        return "    no-timestamp    "
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def fmt_dur(d):
    if d is None:
        return "  ?  "
    m, s = divmod(int(round(d)), 60)
    return f"{m:>3d}m{s:02d}s"


def fmt_delta(d):
    """Format a signed time delta: +27s, +1m30s, 0s."""
    if d is None:
        return "---"
    sign = "+" if d >= 0 else "-"
    m, s = divmod(int(round(abs(d))), 60)
    return f"{sign}{m}m{s:02d}s" if m else f"{sign}{s}s"


def fmt_spread(d):
    """Format an unsigned spread value: 27s, 1m30s, --- if None."""
    if d is None:
        return "---"
    m, s = divmod(int(round(d)), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def mission_warnings(group):
    """Return list of warning flag strings for a mission group."""
    flags = []
    timed = [r for r in group if r.start is not None]
    dured = [r for r in group if r.duration is not None]

    if len(group) == 1:
        flags.append("!1")

    if any(r.duration is not None and r.duration < SHORT_WARN_S for r in group):
        flags.append("!S")

    weak_src = {"mtime", "mtime-span(approx)", "none"}
    if any(r.start_src in weak_src or r.dur_src in weak_src for r in group):
        flags.append("!W")

    if len(timed) >= 2:
        spread = max(r.start for r in timed) - min(r.start for r in timed)
        if spread > START_TOL_S * WARN_TOL_FRAC:
            flags.append("!T")

    if len(dured) >= 2:
        spread = max(r.duration for r in dured) - min(r.duration for r in dured)
        if spread > DUR_TOL_S * WARN_TOL_FRAC:
            flags.append("!D")

    return flags


def cameras_list(recordings):
    return sorted({r.camera for r in recordings})


def suffixed_name(file_entry):
    base = os.path.basename(file_entry.path)
    stem, ext = os.path.splitext(base)
    return f"{stem}_{file_entry.camera}{ext}"


def build_plan(root, missions, orphans, names=None, leftovers=None):
    """Return list of (src, dst) moves."""
    plan = []
    width = len(str(len(missions)))
    for idx, group in enumerate(missions, start=1):
        if names and idx - 1 < len(names):
            mdir = os.path.join(root, f"{MISSION_PREFIX}{names[idx-1]}")
        else:
            mdir = os.path.join(root, f"{MISSION_PREFIX}{idx:0{max(2,width)}d}")
        for rec in group:
            for f in rec.files:
                plan.append((f.path, os.path.join(mdir, suffixed_name(f))))
    other_dir = os.path.join(root, "other")
    for src, cam, _reason in orphans:
        base = os.path.basename(src)
        stem, ext = os.path.splitext(base)
        plan.append((src, os.path.join(other_dir, f"{stem}_{cam}{ext}")))
    if leftovers:
        for rec in leftovers:
            for f in rec.files:
                plan.append((f.path, os.path.join(other_dir, suffixed_name(f))))
    return plan


def print_timeline(recordings):
    print("\n=== SORTED TIMELINE (per-recording) ===")
    print(f"{'start':<21} {'dur':>9}  {'camera':<10} {'video':<6} {'chs':>3}  start/dur source")
    print("-" * 78)
    for r in sorted(recordings, key=lambda r: (r.start is None, r.start or 0)):
        print(f"{fmt_ts(r.start):<21} {fmt_dur(r.duration):>9}  "
              f"{r.camera:<10} {r.video:<6} {r.n_chapters:>3}  "
              f"{r.start_src}/{r.dur_src}")


def _mission_label(idx, names, width):
    if names and idx - 1 < len(names):
        return f"{MISSION_PREFIX}{names[idx-1]}"
    return f"{MISSION_PREFIX}{idx:0{width}d}"


def print_matrix(missions, cameras, names=None):
    """Compact table: one row per mission, one column per camera, cell = video#."""
    col_w = max(18, max((len(c) for c in cameras), default=0) + 6)
    width = max(2, len(str(len(missions))))
    label_w = max(13, max((len(_mission_label(i+1, names, width)) for i in range(len(missions))), default=13) + 1)
    header_cams = "".join(f"{c:<{col_w}}" for c in cameras)
    header = f"{'mission':<{label_w}} {'start':<21} {'dur':>7}  {'warn':<12}{header_cams}"
    print("\n=== MISSION MATRIX ===")
    print(header)
    print("-" * len(header))
    for idx, group in enumerate(missions, start=1):
        anchor = group[0]
        by_cam = {r.camera: r for r in group}
        warn = " ".join(mission_warnings(group)) or ""
        cells = []
        for cam in cameras:
            r = by_cam.get(cam)
            if r is None:
                cells.append(f"{'---':<{col_w}}")
            else:
                cell = f"{r.video} ({r.n_chapters}ch {fmt_dur(r.duration).strip()})"
                cells.append(f"{cell:<{col_w}}")
        label = _mission_label(idx, names, width)
        print(f"{label:<{label_w}} {fmt_ts(anchor.start):<21} {fmt_dur(anchor.duration):>7}  {warn:<12}{''.join(cells)}")


def print_missions_verbose(missions, names=None):
    print("\n=== MISSIONS (verbose) ===")
    width = max(2, len(str(len(missions))))
    for idx, group in enumerate(missions, start=1):
        anchor = group[0]
        cams = ", ".join(sorted(r.camera for r in group))
        label = _mission_label(idx, names, width)
        print(f"\n{label}  anchor {fmt_ts(anchor.start)}  cameras: {cams}")
        for r in sorted(group, key=lambda r: r.camera):
            print(f"    {r.camera:<10} video {r.video}  "
                  f"start {fmt_ts(r.start)}  dur {fmt_dur(r.duration)}  "
                  f"({r.n_chapters} chapter(s), {len(r.files)} files)")


def export_plan_csv(missions, cameras, filepath, names=None):
    """Write annotated mission plan to CSV for human review and editing."""
    import csv
    width = max(2, len(str(len(missions))))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cam_headers = []
    for cam in cameras:
        cam_headers += [cam, f"{cam}_Δt"]

    header = (["mission (edit names)", "start", "# Cameras", "dur",
               "dur_spread", "start_spread", "Short Video",
               "Single Camera", "Near Start Tol", "Near Dur Tol"]
              + cam_headers)

    with open(filepath, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([f"# organize_missions export  {now}"])
        writer.writerow([f"# Tolerances: start +/-{START_TOL_S:.0f}s  |  duration +/-{DUR_TOL_S:.0f}s"])
        writer.writerow([f"# Cameras: {', '.join(cameras)}  |  Missions: {len(missions)}"])
        writer.writerow([])
        writer.writerow(header)

        for idx, group in enumerate(missions, start=1):
            anchor = group[0]
            by_cam = {r.camera: r for r in group}

            if names and idx - 1 < len(names):
                mission_label = names[idx - 1]
            else:
                mission_label = f"{idx:0{width}d}"

            timed = [r for r in group if r.start is not None]
            dured = [r for r in group if r.duration is not None]
            start_spread = (max(r.start for r in timed) - min(r.start for r in timed)
                            if len(timed) >= 2 else None)
            dur_spread = (max(r.duration for r in dured) - min(r.duration for r in dured)
                          if len(dured) >= 2 else None)
            flags = set(mission_warnings(group))

            row = [
                mission_label,
                fmt_ts(anchor.start).strip() if anchor.start else "",
                len(group),
                fmt_dur(anchor.duration).strip() if anchor.duration else "",
                fmt_spread(dur_spread),
                fmt_spread(start_spread),
                "X" if "!S" in flags else "",
                "X" if "!1" in flags else "",
                "X" if "!T" in flags else "",
                "X" if "!D" in flags else "",
            ]
            for cam in cameras:
                r = by_cam.get(cam)
                if r is None:
                    row += ["", "---"]
                else:
                    delta = (r.start - anchor.start) if (r.start is not None and anchor.start is not None) else None
                    row += [r.video, fmt_delta(delta)]
            writer.writerow(row)

    print(f"\nPlan written to: {filepath}")
    print("Edit then re-run with --import <file> to apply.")
    print("  - Only the camera video# cells are read on import; all other columns are informational")
    print("  - Delete a row  → those files stay in place (no move)")
    print("  - Clear a cell  → that camera excluded from that mission")


def import_missions_csv(filepath, recordings):
    """Load missions from an edited CSV. Returns (missions, names).
    Skips preamble comment rows (starting with #) and blank rows.
    Detects camera columns by name so extra diagnostic columns are ignored.
    Video numbers matched as-is first, then zero-padded to 4 digits as fallback."""
    import csv
    rec_map = {(r.camera, r.video): r for r in recordings}
    known_cameras = {r.camera for r in recordings}
    missions, names, seen = [], [], set()
    with open(filepath, newline="") as fh:
        reader = csv.reader(fh)
        # skip preamble and blank rows to find the header
        header = []
        for row in reader:
            if not row or not row[0].strip() or row[0].strip().startswith("#"):
                continue
            header = row
            break
        if not header:
            return [], []
        # find which column indices hold camera video# values (exact name match)
        cam_cols = [(i, col) for i, col in enumerate(header) if col in known_cameras]

        for lineno, row in enumerate(reader, start=2):
            if not row or not row[0].strip() or row[0].strip().startswith("#"):
                continue
            mission_name = row[0].strip()
            group = []
            for col_i, cam in cam_cols:
                video = row[col_i].strip() if col_i < len(row) else ""
                if not video:
                    continue
                # try as-is, then zero-pad to 4 digits
                key = (cam, video)
                if key not in rec_map:
                    key = (cam, video.zfill(4))
                if key in seen:
                    print(f"WARNING line {lineno}: ({cam}, {video}) already assigned — skipping duplicate")
                    continue
                if key not in rec_map:
                    print(f"WARNING line {lineno}: ({cam}, video {video}) not found in footage — skipping")
                    continue
                group.append(rec_map[key])
                seen.add(key)
            if group:
                missions.append(group)
                names.append(mission_name)
    return missions, names


def print_plan(plan, orphans):
    print("\n=== PLANNED MOVES (src -> dst) ===")
    for src, dst in plan:
        print(f"  {src}\n    -> {dst}")
    if orphans:
        print("\n=== UNMATCHED / ORPHAN FILES (-> other/) ===")
        for src, cam, reason in orphans:
            print(f"  [{reason}] {src}")
    print(f"\nTotal files to move: {len(plan)}")


# ---------------------------------------------------------------------------
# Execution (only with --execute)
# ---------------------------------------------------------------------------
def execute(plan):
    # Pre-flight: no destination collisions, all sources exist.
    dsts = {}
    for src, dst in plan:
        if not os.path.exists(src):
            sys.exit(f"ABORT: source vanished: {src}")
        if dst in dsts:
            sys.exit(f"ABORT: two files map to same destination:\n  {dsts[dst]}\n  {src}\n  -> {dst}")
        if os.path.exists(dst):
            sys.exit(f"ABORT: destination already exists: {dst}")
        dsts[dst] = src

    made = set()
    for _, dst in plan:
        d = os.path.dirname(dst)
        if d not in made:
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, MISSION_MARKER), "w").close()
            made.add(d)

    moved = 0
    for src, dst in plan:
        try:
            shutil.move(src, dst)
        except OSError as e:
            # Pre-flight already validated sources/destinations, so a failure here
            # is a system error (permissions, disk, removed media). Abort loudly
            # rather than continue and leave a half-reorganized tree.
            sys.exit(f"\nABORT: move failed after {moved} file(s): {src} -> {dst}\n  {e}")
        moved += 1
    print(f"\nDone. Moved {moved} files.")


# ---------------------------------------------------------------------------
def main():
    global START_TOL_S, DUR_TOL_S
    ap = argparse.ArgumentParser(description="Organize multi-camera GoPro footage into mission folders.")
    ap.add_argument("root", help="Folder containing camera subfolders (left/right/front/...)")
    ap.add_argument("--start-tol", type=float, default=START_TOL_S,
                    help=f"Max start-time gap in seconds (default {START_TOL_S})")
    ap.add_argument("--dur-tol", type=float, default=DUR_TOL_S,
                    help=f"Max duration difference in seconds (default {DUR_TOL_S})")
    ap.add_argument("--timeline", action="store_true",
                    help="Print sorted per-recording timeline with metadata sources")
    ap.add_argument("--verbose", action="store_true",
                    help="Print full per-recording detail inside each mission")
    ap.add_argument("--export", metavar="FILE",
                    help="Export auto-clustered plan to a CSV for manual editing")
    ap.add_argument("--import", dest="import_plan", metavar="FILE",
                    help="Load missions from an edited CSV instead of auto-clustering")
    ap.add_argument("--no-other", action="store_true",
                    help="Leave unassigned recordings in place instead of moving to other/")
    ap.add_argument("--execute", action="store_true", help="Actually move files (default: dry-run)")
    args = ap.parse_args()

    START_TOL_S, DUR_TOL_S = args.start_tol, args.dur_tol
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit(f"Not a directory: {root}")

    recordings, orphans = discover(root)
    cameras = cameras_list(recordings)

    if args.import_plan:
        missions, mission_names = import_missions_csv(args.import_plan, recordings)
        source = f"imported from {args.import_plan}"
    else:
        missions = cluster_missions(recordings)
        mission_names = None
        source = "auto-clustered"

    mission_recs = {id(r) for group in missions for r in group}
    leftovers = None if args.no_other else [r for r in recordings if id(r) not in mission_recs]

    plan = build_plan(root, missions, orphans, names=mission_names, leftovers=leftovers)

    print(f"Root: {root}")
    print(f"Tolerances: start +/-{START_TOL_S:.0f}s, duration +/-{DUR_TOL_S:.0f}s")
    print(f"Cameras found: {cameras or '(none)'}")
    n_left = len(leftovers) if leftovers else 0
    print(f"Recordings: {len(recordings)} | Missions: {len(missions)} ({source}) | Unassigned: {n_left} | Orphans: {len(orphans)}")

    if args.timeline:
        print_timeline(recordings)
    print_matrix(missions, cameras, names=mission_names)
    if args.verbose:
        print_missions_verbose(missions, names=mission_names)
    print_plan(plan, orphans)

    if args.export:
        export_plan_csv(missions, cameras, args.export, names=mission_names)

    if args.execute:
        print("\n--execute set: performing moves...")
        execute(plan)
    else:
        print("\nDRY RUN. No files changed. Re-run with --execute to apply.")


if __name__ == "__main__":
    main()
