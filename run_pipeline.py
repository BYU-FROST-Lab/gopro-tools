#!/usr/bin/env python3
"""
run_pipeline.py — orchestrate the GoPro mission toolchain over a root folder.

Chains the seven processing scripts in dependency order:

  organize → compact → extract_telemetry (+ extract_ros_imu) → sync_gyro
           → crop → overlay_stats

so you can drive a whole folder of missions with one command instead of running
each script by hand. The orchestrator normalizes the scripts' inconsistent
--execute / --dry-run conventions behind a single --execute flag, and pauses at
the two points where a human decision is genuinely required:

  Checkpoint 1 (organize): review/edit mission_plan.csv before any files move.
  Checkpoint 2 (crop):     fill start/end per mission in crop_plan.csv.

Each underlying script is run as a subprocess (with this interpreter), so heavy
or optional dependencies (matplotlib, rosbags, numpy) stay isolated per step.

Usage:
  python run_pipeline.py /root                 # dry-run: show status + planned steps
  python run_pipeline.py /root --status        # just the missions × steps matrix
  python run_pipeline.py /root --execute       # run for real, pausing at checkpoints
  python run_pipeline.py /root --execute --from telemetry --to sync
  python run_pipeline.py /root --execute --only compact
  python run_pipeline.py /root --execute --skip ros,overlay

Per-step settings come from CLI flags or an optional pipeline.yaml at the root
(CLI flag > per-mission YAML override > global YAML > built-in default).
"""

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

import yaml

from utils import (MISSION_MARKER, atomic_write_text, find_missions, is_mission,
                   load_metadata, mission_compacted)
from crop_missions import resolve_offsets

SCRIPT_DIR = Path(__file__).resolve().parent
RESERVED_DIRS = {"other"}

# Canonical pipeline order.
STEP_NAMES = ["organize", "compact", "telemetry", "ros", "sync", "crop", "overlay"]

MISSION_PLAN = "mission_plan.csv"
CROP_PLAN = "crop_plan.csv"
PIPELINE_CFG = "pipeline.yaml"

DEFAULT_CFG = {
    "lrv": False,
    "reencode": False,
    "plots": True,
    "organize": {"start_tol_s": 60.0, "dur_tol_s": 120.0},
    "sync": {"max_lag_s": 30.0, "dt_s": 0.005},
    "ros": {"topic": "/bluerov2/imu/data"},
    # Template for the per-mission overlays.yaml that overlay_stats.py reads.
    # bag / camera / bag_offset_s are filled in automatically per mission; the
    # font_size, line_height and overlays list below are copied verbatim.
    "overlay": {"font_size": 40, "line_height": 50, "overlays": []},
    "missions": {},
}


# ── config ────────────────────────────────────────────────────────────────────

def load_config(root: Path, config_path: Path | None, args) -> dict:
    """Merge built-in defaults < pipeline.yaml < CLI overrides into one config."""
    cfg = json.loads(json.dumps(DEFAULT_CFG))  # deep copy
    path = config_path or (root / PIPELINE_CFG)
    if path.exists():
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v

    # CLI overrides (global). lrv/reencode/plots are opt-in booleans.
    cfg["lrv"] = bool(args.lrv) or bool(cfg.get("lrv"))
    cfg["reencode"] = bool(args.reencode) or bool(cfg.get("reencode"))
    cfg["plots"] = bool(cfg.get("plots", True)) and not args.no_plots
    if args.ros_topic is not None:
        cfg["ros"]["topic"] = args.ros_topic
    # Per-mission max-lag/dt come from YAML; a CLI value (if given) wins for all.
    cfg["_cli_max_lag"] = args.max_lag
    cfg["_cli_dt"] = args.dt
    return cfg


def resolve_sync(cfg: dict, mission_name: str, key: str):
    """Resolve a sync setting: CLI > per-mission YAML > global YAML/default."""
    cli = cfg["_cli_max_lag"] if key == "max_lag_s" else cfg["_cli_dt"]
    if cli is not None:
        return cli
    mov = cfg.get("missions", {}).get(mission_name, {}).get("sync", {})
    if key in mov:
        return mov[key]
    return cfg["sync"][key]


# ── discovery & state ─────────────────────────────────────────────────────────

def subdirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def unorganized_camera_dirs(root: Path) -> list[Path]:
    """Immediate subfolders that look like raw camera folders (unmarked, non-reserved)."""
    if is_mission(root):
        return []
    return [
        d for d in subdirs(root)
        if d.name not in RESERVED_DIRS and not (d / MISSION_MARKER).exists()
    ]


def find_bags(mission: Path) -> list[Path]:
    """ROS bags under a mission (ROS2 dirs with metadata.yaml, or ROS1 .bag files).

    Replicated from extract_ros_imu.find_bags so importing rosbags is not required.
    """
    ros2 = sorted(p.parent for p in mission.rglob("metadata.yaml"))
    bags = list(ros2)
    ros2_set = set(ros2)
    for p in sorted(mission.rglob("*.bag")):
        if not any(p.is_relative_to(d) for d in ros2_set):
            bags.append(p)
    return bags


def topic_name(topic: str) -> str:
    parts = [p for p in topic.split("/") if p]
    return parts[0] if parts else "ros_imu"


def crop_lrv_flag(mission: Path) -> bool | None:
    """The `lrv` flag recorded in the mission's crop.yaml.

    Returns None if the mission hasn't been cropped yet, else the recorded bool
    (a corrupt/half-written crop.yaml reads as False so it's treated as no-LRV).
    """
    p = mission / "crop.yaml"
    if not p.exists():
        return None
    try:
        return bool((yaml.safe_load(p.read_text()) or {}).get("lrv", False))
    except Exception:
        return False


def crop_done(mission: Path, cfg: dict) -> bool:
    """Crop is done only if crop.yaml exists AND it already includes LRV when
    the current run asks for LRV. A mission cropped MP4-only is NOT done under
    --lrv — its proxies still need cutting."""
    lrv = crop_lrv_flag(mission)
    if lrv is None:
        return False
    return lrv or not cfg["lrv"]


def mission_state(mission: Path, cfg: dict) -> dict[str, bool]:
    """Per-mission done/not-done for each pipeline step (read-only checks).

    Each predicate keys off the step's LAST-written artifact so a crashed/partial
    run reads as not-done (and gets re-run) rather than being skipped as complete.
    """
    data = mission / "data"
    meta = load_metadata(mission) or {}

    has_bag = bool(find_bags(mission))
    ros_csv = data / f"{topic_name(cfg['ros']['topic'])}_gyro.csv"
    overlays_dir = mission / "overlays"

    return {
        # raw/ marker is written last by compact; bare raw/ may be a partial run.
        "compact": mission_compacted(mission),
        # metadata.json is written last + atomically by extract_telemetry.
        "telemetry": load_metadata(mission) is not None,
        "ros": (not has_bag) or ros_csv.exists(),
        "sync": bool(meta.get("gyro_offsets_s")),
        "crop": crop_done(mission, cfg),
        "overlay": overlays_dir.is_dir() and any(overlays_dir.glob("*_stats.ass")),
    }


def step_na(mission: Path, cfg: dict) -> dict[str, bool]:
    """Per-mission "step does not apply" flags (shown as — in the status matrix).

    - ros : no ROS bag in the mission.
    - sync: fewer than 2 gyro sources to cross-correlate (e.g. a single-camera
            mission with no bag). Only decided once telemetry has written
            metadata.json, so the camera count is known; otherwise not flagged.
    """
    has_bag = bool(find_bags(mission))
    meta = load_metadata(mission) or {}
    cams = meta.get("cameras", {})
    n_sources = len(cams) + (1 if has_bag else 0)
    return {
        "ros": not has_bag,
        "sync": bool(cams) and n_sources < 2,
    }


# ── command construction & running ────────────────────────────────────────────

def script(name: str) -> str:
    return str(SCRIPT_DIR / name)


def run(cmd: list[str], label: str) -> bool:
    """Run a child script, streaming its output. Return True on success."""
    print(f"\n$ {' '.join(cmd)}", flush=True)
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"  ✗ {label} failed (exit {rc}) — stopping pipeline.")
        return False
    return True


def py(*parts) -> list[str]:
    return [sys.executable, *[str(p) for p in parts]]


# ── crop plan (checkpoint 2) ──────────────────────────────────────────────────

CROP_HEADER = ["mission", "reference_camera", "ref_duration_s",
               "offset_source", "start", "end", "reencode"]


def generate_crop_plan(root: Path, missions: list[Path], path: Path) -> int:
    """Write crop_plan.csv pre-filled from each mission's metadata. Returns row count."""
    rows = []
    for m in missions:
        meta = load_metadata(m)
        if meta is None:
            continue
        ref, offsets = resolve_offsets(meta)
        if ref is None:
            continue
        cam_meta = meta.get("cameras", {}).get(ref, {})
        dur = cam_meta.get("total_duration_s", "")
        if meta.get("gyro_offsets_s"):
            src = "gyro"
        elif meta.get("sync_offsets_s"):
            src = "sync"
        else:
            src = "none"
        rows.append([m.name, ref, dur, src, "", "", str(False).lower()])

    with open(path, "w", newline="") as f:
        f.write("# crop_plan — fill start/end (sec or MM:SS or HH:MM:SS) per mission, "
                "then re-run with --execute.\n")
        f.write("# Blank start/end rows are skipped. reencode: true/false per mission. "
                "LRV comes from the pipeline --lrv flag / pipeline.yaml, not per mission.\n")
        w = csv.writer(f)
        w.writerow(CROP_HEADER)
        w.writerows(rows)
    return len(rows)


def read_crop_plan(path: Path) -> list[dict]:
    """Parse crop_plan.csv, returning rows that have both start and end filled."""
    out = []
    with open(path, newline="") as f:
        reader = csv.DictReader(r for r in f if not r.lstrip().startswith("#"))
        for row in reader:
            if not row.get("mission"):
                continue
            start = (row.get("start") or "").strip()
            end = (row.get("end") or "").strip()
            if not start or not end:
                continue
            out.append({
                "mission": row["mission"].strip(),
                "start": start,
                "end": end,
                "reencode": _truthy(row.get("reencode")),
            })
    return out


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y"}


# ── overlays.yaml generation ───────────────────────────────────────────────────

def resolve_overlay_cfg(cfg: dict, mission_name: str) -> dict:
    """The overlay template for a mission: global overlay cfg < per-mission YAML."""
    base = dict(cfg.get("overlay", {}))
    mov = cfg.get("missions", {}).get(mission_name, {}).get("overlay", {})
    base.update(mov)
    return base


def generate_overlays_yaml(mission: Path, cfg: dict, force: bool) -> str:
    """Write {mission}/overlays.yaml from the pipeline overlay template.

    bag, camera and bag_offset_s are filled in automatically:
      - bag         : the ROS bag discovered in the mission (path relative to it)
      - camera      : the reference camera (offset 0.0 in metadata.json)
      - bag_offset_s: seconds the bag started AFTER the reference camera, read
                      from gyro_offsets_s[<bag source>] in metadata.json (the same
                      value sync_gyro.py computes), falling back to sync_offsets_s.

    The font_size / line_height / overlays list come verbatim from the template,
    so the user edits those once in pipeline.yaml. Returns a short status string.
    """
    ov = resolve_overlay_cfg(cfg, mission.name)
    template = ov.get("overlays") or []
    if not template:
        return "skip:no-template"

    bags = find_bags(mission)
    if not bags:
        return "skip:no-bag"

    out = mission / "overlays.yaml"
    if out.exists() and not force:
        return "exists"

    meta = load_metadata(mission) or {}
    ref, _ = resolve_offsets(meta)
    camera = ref or ov.get("camera")

    # bag_offset_s: prefer gyro, then creation-time sync, then template/default.
    source = topic_name(cfg["ros"]["topic"])
    gyro = meta.get("gyro_offsets_s", {}) or {}
    sync = meta.get("sync_offsets_s", {}) or {}
    if gyro.get(source) is not None:
        bag_offset, off_src = float(gyro[source]), "gyro"
    elif sync.get(source) is not None:
        bag_offset, off_src = float(sync[source]), "sync"
    else:
        bag_offset, off_src = float(ov.get("bag_offset_s", 0.0)), "template/default"

    doc = {
        "bag": str(bags[0].relative_to(mission)),
        "bag_offset_s": round(bag_offset, 6),
        "camera": camera,
        "font_size": ov.get("font_size", 40),
        "line_height": ov.get("line_height", 50),
        "overlays": template,
    }
    header = (
        f"# overlays.yaml — auto-generated by run_pipeline.py for {mission.name}.\n"
        f"# bag/camera/bag_offset_s filled from discovery + metadata.json "
        f"(bag_offset_s source: {off_src}).\n"
        f"# Edit any value by hand; re-run with --force to regenerate from "
        f"pipeline.yaml.\n\n"
    )
    atomic_write_text(out, header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return f"written:{off_src}"


# ── status ────────────────────────────────────────────────────────────────────

def print_status(root: Path, cfg: dict) -> None:
    missions = find_missions(root)
    organized = bool(missions)
    pending_cams = unorganized_camera_dirs(root)

    print(f"\nPipeline status for: {root}")
    print(f"  organize : {'done' if organized and not pending_cams else 'PENDING'}"
          + (f"  ({len(pending_cams)} unorganized camera folder(s))" if pending_cams else ""))

    if not missions:
        print("  (no missions yet — run organize first)")
        return

    steps = ["compact", "telemetry", "ros", "sync", "crop", "overlay"]
    width = max((len(m.name) for m in missions), default=7)
    header = "  " + "mission".ljust(width) + "  " + "  ".join(s[:4] for s in steps)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for m in missions:
        st = mission_state(m, cfg)
        na = step_na(m, cfg)
        cells = []
        for s in steps:
            mark = "—" if na.get(s) else ("✓" if st[s] else "·")
            cells.append(mark.center(4))
        print("  " + m.name.ljust(width) + "  " + "  ".join(cells))
    print("  legend: ✓ done   · pending   — not applicable")


# ── driver ────────────────────────────────────────────────────────────────────

def resolve_selection(args) -> set[str]:
    if args.only:
        return {args.only}
    if args.steps:
        return {s.strip() for s in args.steps.split(",") if s.strip()}
    sel = list(STEP_NAMES)
    if args.from_step:
        sel = sel[STEP_NAMES.index(args.from_step):]
    if args.to_step:
        # slice the (possibly already trimmed) list inclusively up to to_step
        end = STEP_NAMES.index(args.to_step)
        sel = [s for s in sel if STEP_NAMES.index(s) <= end]
    selected = set(sel)
    if args.skip:
        selected -= {s.strip() for s in args.skip.split(",") if s.strip()}
    return selected


def drive(root: Path, cfg: dict, selected: set[str], execute: bool, force: bool) -> None:
    mode = "EXECUTE" if execute else "DRY-RUN"
    print(f"\n=== run_pipeline [{mode}] — steps: {','.join(s for s in STEP_NAMES if s in selected)} ===")

    # ── organize (checkpoint 1) ──
    if "organize" in selected and unorganized_camera_dirs(root):
        plan = root / MISSION_PLAN
        if not plan.exists():
            ok = run(py(script("organize_missions.py"), root, "--export", plan,
                        "--start-tol", cfg["organize"]["start_tol_s"],
                        "--dur-tol", cfg["organize"]["dur_tol_s"]),
                     "organize --export")
            if ok:
                print(f"\n→ CHECKPOINT 1: review/edit {plan}, then re-run with --execute.")
            return
        if not execute:
            print(f"\n→ {plan} exists. Re-run with --execute to import it and continue.")
            return
        if not run(py(script("organize_missions.py"), root, "--import", plan, "--execute",
                      "--start-tol", cfg["organize"]["start_tol_s"],
                      "--dur-tol", cfg["organize"]["dur_tol_s"]),
                   "organize --import"):
            return

    missions = find_missions(root)
    if not missions:
        print("\nNo missions found — nothing further to do.")
        return

    # ── compact (root-level) ──
    if "compact" in selected:
        cmd = py(script("compact_missions.py"), root)
        if execute:
            cmd.append("--execute")
        if cfg["lrv"]:
            cmd.append("--lrv")
        if force:
            cmd.append("--force")
        if not run(cmd, "compact"):
            return

    # ── telemetry (root-level) ──
    if "telemetry" in selected:
        cmd = py(script("extract_telemetry.py"), root)
        if not execute:
            cmd.append("--dry-run")
        if not cfg["plots"]:
            cmd.append("--no-plots")
        if force:
            cmd.append("--force")
        if not run(cmd, "telemetry"):
            return

    # ── ros (root-level; always acts — skipped in dry-run) ──
    if "ros" in selected:
        if not execute:
            print("\n[dry-run] would extract ROS IMU for missions containing a bag "
                  f"(topic {cfg['ros']['topic']}).")
        else:
            cmd = py(script("extract_ros_imu.py"), root, "--topic", cfg["ros"]["topic"])
            if force:
                cmd.append("--force")
            if not run(cmd, "ros"):
                return

    # ── sync (per-mission, to honor per-mission max-lag) ──
    if "sync" in selected:
        for m in missions:
            cmd = py(script("sync_gyro.py"), m,
                     "--max-lag", resolve_sync(cfg, m.name, "max_lag_s"),
                     "--dt", resolve_sync(cfg, m.name, "dt_s"))
            if not execute:
                cmd.append("--dry-run")
            if force:
                cmd.append("--force")
            if not run(cmd, f"sync ({m.name})"):
                return

    # ── crop (checkpoint 2) ──
    if "crop" in selected:
        plan = root / CROP_PLAN
        if not plan.exists():
            n = generate_crop_plan(root, missions, plan)
            print(f"\n→ CHECKPOINT 2: wrote {plan} ({n} mission row(s)). "
                  "Fill start/end per mission, then re-run with --execute.")
            return
        rows = read_crop_plan(plan)
        if not rows:
            print(f"\n{plan} has no filled start/end rows — nothing to crop.")
        for row in rows:
            mdir = root / row["mission"]
            # crop_missions.py self-gates: it skips a mission already cropped to
            # this window, adds LRV proxies when crop.yaml has lrv:false, and
            # requires --force to re-cut a different window. We just invoke it.
            cmd = py(script("crop_missions.py"), mdir,
                     "--start", row["start"], "--end", row["end"])
            if cfg["lrv"]:
                cmd.append("--lrv")
            if cfg["reencode"] or row["reencode"]:
                cmd.append("--reencode")
            if execute:
                cmd.append("--execute")
            if force:
                cmd.append("--force")
            if not run(cmd, f"crop ({row['mission']})"):
                return

    # ── overlay (per-mission) ──
    # Each mission's overlays.yaml is generated from the pipeline overlay template
    # (bag/camera/bag_offset_s auto-filled), then overlay_stats.py renders the .ass.
    if "overlay" in selected:
        for m in missions:
            status = generate_overlays_yaml(m, cfg, force)
            if status.startswith("written"):
                src = status.split(":", 1)[1]
                print(f"\n[overlay] generated {m.name}/overlays.yaml from template "
                      f"(bag_offset_s from {src}). Edit it to customize.")
            elif status == "exists":
                print(f"\n[overlay] {m.name}/overlays.yaml present (kept; "
                      "--force regenerates from pipeline.yaml).")
            # skip:no-template / skip:no-bag fall through — render only if a
            # hand-written overlays.yaml already exists for the mission.
            if not (m / "overlays.yaml").exists():
                continue
            cmd = py(script("overlay_stats.py"), m)
            if not execute:
                cmd.append("--dry-run")
            if force:
                cmd.append("--force")
            if not run(cmd, f"overlay ({m.name})"):
                return

    print(f"\n=== run_pipeline [{mode}] complete ===")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Orchestrate the GoPro mission toolchain over a root folder.")
    ap.add_argument("root", type=Path,
                    help="Folder of camera subfolders / missions (or a single mission folder)")
    ap.add_argument("--execute", action="store_true",
                    help="Run for real (default: dry-run / plan only)")
    ap.add_argument("--status", action="store_true",
                    help="Print the missions × steps status matrix and exit")

    sel = ap.add_argument_group("step selection")
    sel.add_argument("--steps", help="Comma list of steps to run (e.g. compact,telemetry)")
    sel.add_argument("--only", choices=STEP_NAMES, help="Run only this step")
    sel.add_argument("--from", dest="from_step", choices=STEP_NAMES,
                     help="Start from this step (inclusive)")
    sel.add_argument("--to", dest="to_step", choices=STEP_NAMES,
                     help="Stop after this step (inclusive)")
    sel.add_argument("--skip", help="Comma list of steps to skip")

    cfg = ap.add_argument_group("settings (override pipeline.yaml)")
    cfg.add_argument("--force", action="store_true",
                     help="Re-run steps even if already done (passes --force to children)")
    cfg.add_argument("--lrv", action="store_true", help="Also process LRV proxies")
    cfg.add_argument("--reencode", action="store_true",
                     help="Frame-accurate crop via re-encode")
    cfg.add_argument("--no-plots", action="store_true", help="Skip telemetry plot generation")
    cfg.add_argument("--max-lag", type=float, default=None,
                     help="sync_gyro max offset search (s); overrides config for all missions")
    cfg.add_argument("--dt", type=float, default=None,
                     help="sync_gyro resample interval (s)")
    cfg.add_argument("--ros-topic", default=None, help="ROS IMU topic to extract")
    cfg.add_argument("--config", type=Path, default=None,
                     help=f"Pipeline config (default: {{root}}/{PIPELINE_CFG})")

    args = ap.parse_args()
    root = args.root
    if not root.is_dir():
        sys.exit(f"error: not a directory: {root}")

    config = load_config(root, args.config, args)

    if args.status:
        print_status(root, config)
        return

    print_status(root, config)
    selected = resolve_selection(args)
    drive(root, config, selected, args.execute, args.force)


if __name__ == "__main__":
    main()
