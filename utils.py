#!/usr/bin/env python3
"""
utils.py — Shared utilities for GoPro mission tools.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MISSION_MARKER = ".gopro_mission"

# GoPro filename: prefix(GX/GL/GH) + 2-digit chapter + 4-digit video number + ext
NAME_RE = re.compile(r"^(?P<prefix>G[XLH])(?P<chapter>\d{2})(?P<video>\d{4})\.(?P<ext>[A-Za-z0-9]+)$")

# Organized filename (post-organize): same, with a _{camera} suffix before the ext.
COMPACT_RE = re.compile(
    r"^(?P<prefix>G[XLH])(?P<chapter>\d{2})(?P<video>\d{4})_(?P<camera>[^.]+)\.(?P<ext>[A-Za-z0-9]+)$")

# Compacted filename (post-compact): just {camera}.MP4 / {camera}.LRV, no chapter/video#.
SIMPLE_RE = re.compile(r"^(?P<camera>[A-Za-z][A-Za-z0-9_]*)\.(?P<ext>MP4|LRV)$", re.IGNORECASE)

VIDEO_EXTS = {"MP4"}
PROXY_EXTS = {"LRV"}
THUMB_EXTS = {"THM"}


@dataclass
class FileEntry:
    path: str
    camera: str
    prefix: str
    chapter: int
    video: str      # 4-char string — preserves leading zeros
    ext: str        # upper-cased


@dataclass
class Recording:
    """One continuous recording from one camera: all chapters sharing video#."""
    camera: str
    video: str
    files: list = field(default_factory=list)   # list[FileEntry]
    start: float = None                          # epoch seconds (best available)
    start_src: str = ""                          # which fallback provided start
    duration: float = None                       # seconds (sum over chapters)
    dur_src: str = ""

    @property
    def n_chapters(self):
        return len({f.chapter for f in self.files if f.ext in VIDEO_EXTS})

    @property
    def label(self):
        return f"{self.camera}:{self.video}"


# ---------------------------------------------------------------------------
# Metadata: ffprobe -> THM embedded -> filesystem mtime
# ---------------------------------------------------------------------------

def ffprobe(path):
    """Return (creation_epoch|None, duration_s|None). Never raises."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None, None
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None, None

    fmt = data.get("format", {})
    dur = None
    try:
        dur = float(fmt.get("duration"))
    except (TypeError, ValueError):
        for s in data.get("streams", []):
            try:
                dur = float(s.get("duration"))
                break
            except (TypeError, ValueError):
                continue

    ct = (fmt.get("tags", {}) or {}).get("creation_time")
    start = _parse_iso(ct)
    if start is None:
        for s in data.get("streams", []):
            ct = (s.get("tags", {}) or {}).get("creation_time")
            start = _parse_iso(ct)
            if start is not None:
                break
    return start, dur


def _parse_iso(s):
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def thm_creation(path):
    """Try to read a creation time from a THM (small JPEG w/ GoPro metadata)."""
    start, _ = ffprobe(path)
    return start


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def best_start(rec):
    """Earliest chapter's start via fallback chain. Returns (epoch, src_string)."""
    chapters = sorted(
        [f for f in rec.files if f.ext in VIDEO_EXTS],
        key=lambda f: f.chapter,
    )
    if not chapters:
        chapters = sorted(rec.files, key=lambda f: f.chapter)
    first = chapters[0]

    s, _ = ffprobe(first.path)
    if s is not None:
        return s, "ffprobe"

    thm = next((f for f in rec.files if f.ext in THUMB_EXTS and f.chapter == first.chapter), None)
    if thm:
        s = thm_creation(thm.path)
        if s is not None:
            return s, "thm"

    s = mtime(first.path)
    if s is not None:
        return s, "mtime"
    return None, "none"


def total_duration(rec):
    """Sum ffprobe durations across MP4 chapters. Falls back to mtime spacing."""
    vids = sorted([f for f in rec.files if f.ext in VIDEO_EXTS], key=lambda f: f.chapter)
    total, used_ffprobe, missing = 0.0, False, False
    for f in vids:
        _, d = ffprobe(f.path)
        if d is not None:
            total += d
            used_ffprobe = True
        else:
            missing = True
    if used_ffprobe and not missing:
        return total, "ffprobe"
    if used_ffprobe and missing:
        return total, "ffprobe(partial)"

    # No ffprobe durations at all: approximate from mtime spacing of chapters.
    mts = [mtime(f.path) for f in vids]
    mts = [m for m in mts if m is not None]
    if len(mts) >= 2:
        return max(mts) - min(mts), "mtime-span(approx)"
    return None, "none"


# ---------------------------------------------------------------------------
# Atomic writes — every generated file is written to a sibling .tmp then
# os.replace()'d into place, so a crash never leaves a truncated final file.
# ---------------------------------------------------------------------------

def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    """Atomically write text to path (write sibling .tmp, then os.replace)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def atomic_write_bytes(path, data: bytes) -> None:
    """Atomically write bytes to path (write sibling .tmp, then os.replace)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Mission discovery and metadata.json I/O — shared by all pipeline scripts.
# ---------------------------------------------------------------------------

def is_mission(path) -> bool:
    """A folder is a mission if it carries the marker or already has metadata.json."""
    path = Path(path)
    return (path / MISSION_MARKER).exists() or (path / "data" / "metadata.json").exists()


def find_missions(root, exit_on_empty: bool = False) -> list:
    """Return mission folders under root.

    If root itself is a mission, return [root]; otherwise return immediate
    subfolders carrying the .gopro_mission marker. With exit_on_empty=True,
    sys.exit with a message when none are found (CLI convenience).
    """
    root = Path(root)
    if is_mission(root):
        return [root]
    missions = sorted(p.parent for p in root.glob(f"*/{MISSION_MARKER}"))
    if not missions and exit_on_empty:
        sys.exit(
            f"error: no missions found under {root}\n"
            f"  (no {MISSION_MARKER} markers in immediate subfolders)"
        )
    return missions


def mission_compacted(mission) -> bool:
    """Compact completion sentinel: raw/ exists AND carries the marker (written last)."""
    return (Path(mission) / "raw" / MISSION_MARKER).exists()


def load_metadata(mission, default=None):
    """Load mission/data/metadata.json. Return default on missing or unreadable.

    Never raises — a corrupt/half-written metadata.json reads as default so callers
    treat the step as not-yet-done rather than crashing.
    """
    path = Path(mission) / "data" / "metadata.json"
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def save_metadata(mission, meta: dict) -> None:
    """Atomically write mission/data/metadata.json (creates data/ if needed)."""
    data_dir = Path(mission) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(data_dir / "metadata.json", json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Time parsing/formatting (reference-timeline windows, crop records).
# ---------------------------------------------------------------------------

def parse_time(s: str) -> float:
    """Parse seconds ('123.5'), MM:SS ('3:20'), or HH:MM:SS ('1:03:20') -> seconds."""
    s = s.strip()
    if ":" in s:
        parts = s.split(":")
        if len(parts) > 3:
            raise ValueError(f"invalid time: {s}")
        sec = 0.0
        for p in parts:
            sec = sec * 60 + float(p)
        return sec
    return float(s)


def fmt_time(t: float) -> str:
    """Format seconds as M:SS.mmm or H:MM:SS.mmm."""
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    if h:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"
