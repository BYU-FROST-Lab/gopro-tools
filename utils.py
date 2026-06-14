#!/usr/bin/env python3
"""
utils.py — Shared utilities for GoPro mission tools.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

MISSION_MARKER = ".gopro_mission"

# GoPro filename: prefix(GX/GL/GH) + 2-digit chapter + 4-digit video number + ext
NAME_RE = re.compile(r"^(?P<prefix>G[XLH])(?P<chapter>\d{2})(?P<video>\d{4})\.(?P<ext>[A-Za-z0-9]+)$")

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
