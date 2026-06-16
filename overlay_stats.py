#!/usr/bin/env python3
"""
overlay_stats.py — overlay ROS bag data on GoPro video as an ASS subtitle file.

Reads any topic/field from a ROS bag and generates a subtitle file that displays
values as text over the video. No re-encoding needed — load the .ass file in VLC,
mpv, or DaVinci Resolve alongside the original video.

Config is a YAML file specifying the bag, offset, and a list of overlays. Each
overlay picks a ROS topic and a field (dot-notation for nested structs).

Usage:
  python overlay_stats.py MISSION_DIR [--config overlays.yaml]
  python overlay_stats.py MISSION_DIR --list-topics
  python overlay_stats.py MISSION_DIR --start 200 --end 400   # subclip
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import numpy as np
import yaml
from rosbags.highlevel import AnyReader

from utils import load_metadata, atomic_write_text


# ── data structures ────────────────────────────────────────────────────────────

@dataclass
class OverlaySpec:
    topic: str
    field: str
    label: str
    unit: str
    fmt: str
    color: str
    anchor: str       # "top-left" | "custom"
    x: int = 0
    y: int = 0
    an: int = 7       # ASS numpad alignment (7=top-left, 9=top-right, etc.)
    stack_index: int = 0
    style_name: str = ""


# Named anchors: (base_x, base_y, ASS \an value, bottom_stacking)
# base_x/y of None means "use video edge minus margin"
_ANCHORS: dict[str, tuple] = {
    "top-left":      (10,   10,   7, False),
    "top-center":    (None, 10,   8, False),
    "top-right":     (None, 10,   9, False),
    "bottom-left":   (10,   None, 1, True),
    "bottom-center": (None, None, 2, True),
    "bottom-right":  (None, None, 3, True),
}
_MARGIN = 10


# ── config loading ─────────────────────────────────────────────────────────────

def load_config(config_path: Path, mission_dir: Path) -> dict:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    # Resolve bag path
    bag_raw = cfg.get("bag")
    if bag_raw is None:
        sys.exit("error: 'bag' key required in config (path to ROS bag directory or file)")
    p = Path(bag_raw)
    if not p.is_absolute():
        p = mission_dir / p
    cfg["bag_path"] = p

    cfg.setdefault("bag_offset_s", 0.0)
    cfg.setdefault("camera", None)
    cfg.setdefault("font_size", 28)
    cfg.setdefault("line_height", 34)
    cfg.setdefault("overlays", [])
    return cfg


def build_specs(cfg: dict) -> list[OverlaySpec]:
    color_to_style: dict[str, str] = {}
    specs: list[OverlaySpec] = []

    for oc in cfg["overlays"]:
        if not oc.get("enabled", True):
            continue
        topic = oc.get("topic")
        field = oc.get("field")
        if not topic or not field:
            sys.exit(f"error: each overlay needs 'topic' and 'field'; got: {oc}")

        color = oc.get("color", "#FFFFFF")
        if color not in color_to_style:
            color_to_style[color] = f"ov{len(color_to_style)}"

        pos = oc.get("position", "top-left")
        if isinstance(pos, list) and len(pos) == 2:
            spec = OverlaySpec(
                topic=topic, field=field,
                label=oc.get("label", field),
                unit=oc.get("unit", ""),
                fmt=oc.get("format", ".3f"),
                color=color,
                anchor="custom",
                x=int(pos[0]), y=int(pos[1]),
                an=7,
                style_name=color_to_style[color],
            )
        elif pos in _ANCHORS:
            spec = OverlaySpec(
                topic=topic, field=field,
                label=oc.get("label", field),
                unit=oc.get("unit", ""),
                fmt=oc.get("format", ".3f"),
                color=color,
                anchor=pos,
                style_name=color_to_style[color],
            )
        else:
            sys.exit(f"error: unknown position '{pos}'. Use a named anchor or [x, y] list.")

        specs.append(spec)

    return specs


def resolve_positions(specs: list[OverlaySpec], W: int, H: int, line_height: int) -> None:
    """Set x, y, an, stack_index on each spec in-place."""
    anchor_count: dict[str, int] = {}
    for spec in specs:
        if spec.anchor == "custom":
            spec.an = 7
            continue
        base_x, base_y, an, bottom = _ANCHORS[spec.anchor]
        spec.an = an
        spec.stack_index = anchor_count.get(spec.anchor, 0)
        anchor_count[spec.anchor] = spec.stack_index + 1

        x = (W // 2) if base_x is None else base_x
        y = (H - _MARGIN) if base_y is None else base_y

        if bottom:
            spec.y = y - spec.stack_index * line_height
        else:
            spec.y = y + spec.stack_index * line_height
        spec.x = x


# ── video probing ──────────────────────────────────────────────────────────────

def probe_video(path: Path) -> dict:
    """Return dict: width, height, fps (float), duration_s."""
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        sys.exit(f"error: ffprobe failed on {path}")
    data = json.loads(out.stdout)

    vs = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if vs is None:
        sys.exit(f"error: no video stream in {path}")

    W, H = int(vs["width"]), int(vs["height"])
    num, den = (int(x) for x in vs.get("avg_frame_rate", "24/1").split("/"))
    fps = num / den
    dur = float(data.get("format", {}).get("duration", 0))
    return {"width": W, "height": H, "fps": fps, "duration_s": dur}


def find_video(mission_dir: Path, camera: str) -> Path | None:
    candidates = [mission_dir / f"{camera}.MP4"]
    candidates += sorted(mission_dir.glob(f"GX*_{camera}.MP4"))
    return next((p for p in candidates if p.exists()), None)


# ── bag reading ────────────────────────────────────────────────────────────────

def get_nested_field(msg, field_path: str) -> float:
    """Access a nested message field via dot notation, e.g. 'angular_velocity.x'."""
    obj = msg
    for part in field_path.split("."):
        if "[" in part:
            name, idx = part.rstrip("]").split("[")
            obj = getattr(obj, name)[int(idx)]
        else:
            obj = getattr(obj, part)
    return float(obj)


def get_stamp_s(msg, bag_ts_ns: int) -> float:
    """Return message timestamp in seconds; prefers header.stamp over bag record time."""
    try:
        s = msg.header.stamp
        return s.sec + s.nanosec * 1e-9
    except AttributeError:
        return bag_ts_ns * 1e-9


def load_bag_topics(bag_path: Path, topic_fields: dict[str, list[str]]) -> dict:
    """
    Load the requested topics from the bag.
    Returns {topic: {"t_s": ndarray, field: ndarray, ...}} with t_s starting at 0.
    """
    raw: dict[str, dict[str, list]] = {
        t: {"t_s": [], **{f: [] for f in fields}}
        for t, fields in topic_fields.items()
    }

    with AnyReader([bag_path]) as reader:
        available = {c.topic for c in reader.connections}
        missing = set(topic_fields) - available
        if missing:
            print("  Available topics:")
            for t in sorted(available):
                print(f"    {t}")
            sys.exit(f"error: topic(s) not found in bag: {', '.join(sorted(missing))}")

        conns = [c for c in reader.connections if c.topic in topic_fields]
        for conn, bag_ts, rawdata in reader.messages(connections=conns):
            topic = conn.topic
            msg = reader.deserialize(rawdata, conn.msgtype)
            raw[topic]["t_s"].append(get_stamp_s(msg, bag_ts))
            for f in topic_fields[topic]:
                try:
                    raw[topic][f].append(get_nested_field(msg, f))
                except (AttributeError, IndexError, TypeError):
                    raw[topic][f].append(float("nan"))

    out: dict = {}
    for topic, arrays in raw.items():
        t = np.array(arrays["t_s"])
        if len(t) == 0:
            sys.exit(f"error: no messages received on topic '{topic}'")
        t -= t[0]  # normalize to start at 0
        out[topic] = {"t_s": t}
        for f in topic_fields[topic]:
            out[topic][f] = np.array(arrays[f])

    return out


# ── list-topics mode ───────────────────────────────────────────────────────────

def list_topics(bag_path: Path) -> None:
    with AnyReader([bag_path]) as reader:
        seen: dict[str, str] = {}
        for c in reader.connections:
            seen[c.topic] = c.msgtype

    print(f"\nTopics in {bag_path.name}:")
    print(f"  {'Topic':<52} Type")
    print(f"  {'-'*52} {'-'*40}")
    for topic in sorted(seen):
        print(f"  {topic:<52} {seen[topic]}")


# ── ASS generation ─────────────────────────────────────────────────────────────

def _html_to_ass(html: str) -> str:
    """#RRGGBB → &H00BBGGRR& (ASS byte order)."""
    r, g, b = int(html[1:3], 16), int(html[3:5], 16), int(html[5:7], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def _to_ass_ts(t_s: float) -> str:
    """Float seconds → ASS timestamp h:mm:ss.cs (centiseconds)."""
    t_s = max(0.0, t_s)
    h = int(t_s // 3600)
    m = int((t_s % 3600) // 60)
    s = int(t_s % 60)
    cs = int((t_s % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_header(W: int, H: int, specs: list[OverlaySpec], font_size: int) -> str:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "Timer: 100.0000",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    seen: set[str] = set()
    for spec in specs:
        if spec.style_name in seen:
            continue
        seen.add(spec.style_name)
        pc = _html_to_ass(spec.color)
        lines.append(
            f"Style: {spec.style_name},DejaVu Sans Mono,{font_size},"
            f"{pc},&H000000FF&,&H00000000&,&H80000000&,"
            f"0,0,0,0,100,100,0,0,1,2,1,7,10,10,10,1"
        )
    lines += ["", "[Events]",
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
              ""]
    return "\n".join(lines)


def generate_ass(
    out_path: Path,
    specs: list[OverlaySpec],
    topic_data: dict,
    fps: float,
    bag_offset_s: float,
    clip_start_s: float,
    clip_end_s: float,
    W: int, H: int,
    font_size: int,
    line_height: int,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dt = 1.0 / fps
    n_frames = int((clip_end_s - clip_start_s) * fps) + 2

    # Run-length encoding state per overlay
    prev_text: list[str | None] = [None] * len(specs)
    event_start: list[float] = [0.0] * len(specs)
    events: list[str] = []

    def flush(i: int, t_video_abs: float) -> None:
        if prev_text[i] is None:
            return
        t_s = event_start[i] - clip_start_s
        t_e = t_video_abs - clip_start_s
        if t_e <= 0:
            return
        t_s = max(0.0, t_s)
        spec = specs[i]
        events.append(
            f"Dialogue: 0,{_to_ass_ts(t_s)},{_to_ass_ts(t_e)},{spec.style_name},,0,0,0,,"
            f"{{\\an{spec.an}\\pos({spec.x},{spec.y})}}{prev_text[i]}"
        )

    for frame in range(n_frames):
        t_video = clip_start_s + frame * dt
        if t_video > clip_end_s + dt:
            break

        for i, spec in enumerate(specs):
            t_bag = t_video - bag_offset_s

            t_arr = topic_data[spec.topic]["t_s"]
            v_arr = topic_data[spec.topic][spec.field]

            if t_bag < 0.0 or t_bag > t_arr[-1]:
                text = f"{spec.label}: N/A"
            else:
                val = float(np.interp(t_bag, t_arr, v_arr))
                text = f"{spec.label}: {format(val, spec.fmt)} {spec.unit}"

            if text != prev_text[i]:
                flush(i, t_video)
                prev_text[i] = text
                event_start[i] = t_video

    for i in range(len(specs)):
        flush(i, clip_end_s)

    text = _ass_header(W, H, specs, font_size) + "".join(ev + "\n" for ev in events)
    atomic_write_text(out_path, text)

    print(f"  → {out_path}  ({len(events)} dialogue events)")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("mission", type=Path, help="mission folder")
    ap.add_argument("--config", type=Path, default=None,
                    help="YAML overlay config (default: {mission}/overlays.yaml)")
    ap.add_argument("--camera", default=None,
                    help="camera name (overrides config; used to find video file)")
    ap.add_argument("--start", type=float, default=None, metavar="T",
                    help="subclip start in seconds (ASS timestamps shifted to t=0); "
                         "use against the FULL video before cropping")
    ap.add_argument("--end", type=float, default=None, metavar="T",
                    help="subclip end in seconds")
    ap.add_argument("--crop-offset", type=float, default=0.0, metavar="T",
                    help="seconds into the original reference timeline where this "
                         "already-cropped video begins (i.e. the crop --start used by "
                         "crop_missions.py); subtracted from bag_offset_s so the overlay "
                         "aligns with the cropped clip")
    ap.add_argument("--list-topics", action="store_true",
                    help="list all topics in the bag and exit")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing output file")
    ap.add_argument("--dry-run", action="store_true",
                    help="print plan without writing any files")
    args = ap.parse_args()

    mission_dir: Path = args.mission
    if not mission_dir.exists():
        sys.exit(f"error: mission folder not found: {mission_dir}")

    config_path = args.config or mission_dir / "overlays.yaml"
    if not config_path.exists():
        sys.exit(
            f"error: config not found: {config_path}\n"
            f"Create one or pass --config PATH"
        )

    cfg = load_config(config_path, mission_dir)
    bag_path: Path = cfg["bag_path"]
    if not bag_path.exists():
        sys.exit(f"error: bag not found: {bag_path}")

    if args.list_topics:
        list_topics(bag_path)
        return

    # Resolve camera
    camera = args.camera or cfg.get("camera")
    if camera is None:
        meta = load_metadata(mission_dir)
        if meta:
            offsets = meta.get("gyro_offsets_s", meta.get("sync_offsets_s", {}))
            camera = next((c for c, v in offsets.items() if v == 0.0), None)
            if camera is None and meta.get("cameras"):
                camera = sorted(meta["cameras"])[0]
    if camera is None:
        sys.exit("error: set 'camera' in config or use --camera")

    bag_offset_s = float(cfg.get("bag_offset_s", 0.0))
    if args.crop_offset:
        # Cropped video starts crop_offset seconds into the original reference
        # timeline, so the bag is effectively (bag_offset - crop_offset) seconds away.
        bag_offset_s -= args.crop_offset
    font_size = int(cfg.get("font_size", 28))
    line_height = int(cfg.get("line_height", 34))

    specs = build_specs(cfg)
    if not specs:
        sys.exit("error: no overlays defined in config (or all disabled)")

    # Find and probe video
    video_path = find_video(mission_dir, camera)
    if video_path is None:
        sys.exit(f"error: no video file found for camera '{camera}' in {mission_dir}")

    print(f"Video: {video_path.name}")
    vinfo = probe_video(video_path)
    W, H, fps, dur = vinfo["width"], vinfo["height"], vinfo["fps"], vinfo["duration_s"]
    print(f"  {W}×{H}  {fps:.3f} fps  {dur:.1f} s")

    clip_start = args.start if args.start is not None else 0.0
    clip_end = args.end if args.end is not None else dur
    clip_end = min(clip_end, dur)

    resolve_positions(specs, W, H, line_height)

    # Summary
    if args.crop_offset:
        print(f"\nCrop offset: {args.crop_offset:+.3f} s  → effective bag offset "
              f"{bag_offset_s:+.3f} s (from {bag_offset_s + args.crop_offset:+.3f} s)")
    print(f"\nBag: {bag_path.name}  (offset {bag_offset_s:+.3f} s from t=0 on video)")
    if args.start is not None or args.end is not None:
        print(f"Subclip: {clip_start:.1f}s – {clip_end:.1f}s  (ASS timestamps start at 0:00:00.00)")
    print(f"\nOverlays ({len(specs)}):")
    for spec in specs:
        print(f"  [{spec.anchor}]  {spec.topic} → {spec.field}  \"{spec.label}\"")

    if args.dry_run:
        print("\n(dry-run — no files written)")
        return

    out_path = mission_dir / "overlays" / f"{camera}_stats.ass"
    if out_path.exists() and not args.force:
        sys.exit(f"error: output exists: {out_path}  (use --force to overwrite)")

    # Collect unique topic→fields mapping
    topic_fields: dict[str, list[str]] = {}
    for spec in specs:
        topic_fields.setdefault(spec.topic, [])
        if spec.field not in topic_fields[spec.topic]:
            topic_fields[spec.topic].append(spec.field)

    print(f"\nReading {len(topic_fields)} topic(s) from bag ...")
    topic_data = load_bag_topics(bag_path, topic_fields)
    for topic, arrays in topic_data.items():
        n = len(arrays["t_s"])
        bag_dur = float(arrays["t_s"][-1]) if n > 0 else 0.0
        print(f"  {topic}: {n} msgs  {bag_dur:.1f} s")

    print("\nGenerating ASS file ...")
    generate_ass(
        out_path, specs, topic_data, fps,
        bag_offset_s, clip_start, clip_end,
        W, H, font_size, line_height,
    )

    print("\nTo view:")
    lrv = next(
        (p for p in [mission_dir / f"{camera}_LRV.MP4",
                     *sorted(mission_dir.glob(f"GL*_{camera}_LRV.MP4"))]
         if p.exists()),
        None,
    )
    play_path = lrv if lrv else video_path
    print(f"  mpv \"{play_path}\" --sub-file=\"{out_path}\"")
    print("Done.")


if __name__ == "__main__":
    main()
