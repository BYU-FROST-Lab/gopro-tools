#!/usr/bin/env python3
"""
extract_telemetry.py — Extract and plot GoPro GPMF telemetry from organized missions.

Reads GPMF streams from raw chapter MP4 files, saves data as CSV (and GPX when
GPS is available), then generates diagnostic PNG plots in {mission}/data/plots/.
Also writes metadata.json with file sizes, durations, and creation times for
each camera — useful for quick inspection before downloading large files.

Usage:
  python3 extract_telemetry.py <root_or_mission> [options]

Examples:
  python3 extract_telemetry.py /media/Frostlab/SandHollow/
  python3 extract_telemetry.py /media/Frostlab/SandHollow/DiveArea/
  python3 extract_telemetry.py /media/Frostlab/SandHollow/ --mission Ball
  python3 extract_telemetry.py /media/Frostlab/SandHollow/ --no-plots --dry-run
"""

import argparse
import csv
import json
import os
import re
import struct
import subprocess
import sys
import io
from pathlib import Path
from datetime import datetime, timezone

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("Warning: matplotlib/numpy not installed — plots will be skipped.", file=sys.stderr)

from utils import MISSION_MARKER, ffprobe

# Post-organization filename: GX010149_Front.MP4
COMPACT_RE = re.compile(
    r"^(?P<prefix>G[XLH])(?P<chapter>\d{2})(?P<video>\d{4})_(?P<camera>[^.]+)\.(?P<ext>[A-Za-z0-9]+)$"
)
# Compacted filename: Front.MP4
SIMPLE_RE = re.compile(r"^(?P<camera>[A-Za-z][A-Za-z0-9_]*)\.(?P<ext>MP4|LRV)$", re.IGNORECASE)

# Streams to extract: key → (human name, columns)
KNOWN_STREAMS = {
    "ACCL": ("Accelerometer", ["ax_ms2", "ay_ms2", "az_ms2"]),
    "GYRO": ("Gyroscope",     ["gx_rads", "gy_rads", "gz_rads"]),
    "GRAV": ("Gravity",       ["gx", "gy", "gz"]),
    "CORI": ("CameraOrient",  ["w", "x", "y", "z"]),
    "IORI": ("ImageOrient",   ["w", "x", "y", "z"]),
    "GPS5": ("GPS",           ["lat_deg", "lon_deg", "alt_m", "speed_ms", "accuracy_m"]),
    "GPS9": ("GPS9",          ["lat_deg", "lon_deg", "alt_m", "speed_2d", "speed_3d", "unk1", "unk2", "unk3", "unk4"]),
    # Exposure telemetry — averaged into video_settings, no CSV written
    "SHUT": ("Shutter",     ["shutter_s"]),
    "ISOE": ("ISO",         ["iso"]),
    "TMPC": ("Temperature", ["temp_c"]),
}

# KNOWN_STREAMS keys that produce metadata averages instead of CSV files
METADATA_ONLY_STREAMS = {"SHUT", "ISOE", "TMPC"}

# udta/GPMF PRJT (projection) fourcc → human lens-mode name
_PRJT_LENS_MODES = {
    "GPRO": "Wide",
    "SPRO": "SuperView",
    "LINR": "Linear",
    "NHZN": "Linear+Horizon Lock",
    "WFOV": "HyperView",
    "NFOV": "Narrow",
}

# ── GPMF binary parsing ───────────────────────────────────────────────────────

_FMT = {
    ord('b'): (1, '>b'), ord('B'): (1, '>B'),
    ord('s'): (2, '>h'), ord('S'): (2, '>H'),
    ord('l'): (4, '>i'), ord('L'): (4, '>I'),
    ord('f'): (4, '>f'), ord('d'): (8, '>d'),
    ord('j'): (8, '>q'), ord('J'): (8, '>Q'),
}


def _parse_klvs(data: bytes, offset: int = 0):
    """Yield (key_str, type_byte, elem_size, repeat, payload_bytes)."""
    while offset + 8 <= len(data):
        key = data[offset:offset + 4].decode("ascii", errors="replace")
        type_byte = data[offset + 4]
        size = data[offset + 5]
        repeat = struct.unpack(">H", data[offset + 6:offset + 8])[0]
        payload_len = size * repeat
        padded = (payload_len + 3) & ~3
        payload = data[offset + 8:offset + 8 + payload_len]
        offset += 8 + padded
        if key.strip("\x00"):
            yield key, type_byte, size, repeat, payload


def _decode_row(type_byte: int, elem_size: int, n_elems: int, row_bytes: bytes):
    """Decode one row of n_elems values from raw bytes."""
    info = _FMT.get(type_byte)
    if info is None:
        return None
    unit_size, fmt = info
    n = min(n_elems, elem_size // unit_size)
    return [struct.unpack(fmt, row_bytes[i * unit_size:(i + 1) * unit_size])[0]
            for i in range(n)]


def parse_gpmf_packets(raw: bytes) -> list:
    """
    Parse raw GPMF binary into a list of packet dicts, one per DEVC block.

    Each packet:
      {
        'streams': {
          'ACCL': {'stmp_us': int, 'tsmp': int, 'scal': float|list, 'rows': [[v,...], ...]},
          ...
        }
      }
    """
    packets = []
    for key, tb, sz, rp, payload in _parse_klvs(raw):
        if key != "DEVC":
            continue
        pkt = {"streams": {}}
        for dkey, dtb, dsz, drp, dpayload in _parse_klvs(payload):
            if dkey != "STRM":
                continue
            strm: dict = {"stmp_us": 0, "tsmp": 0, "scal": 1, "stnm": "", "rows": [], "data_key": ""}
            for skey, stb, ssz, srp, sp in _parse_klvs(dpayload):
                if skey == "STNM":
                    strm["stnm"] = sp.decode("ascii", errors="replace").rstrip("\x00")
                elif skey == "STMP":
                    strm["stmp_us"] = struct.unpack(">Q", sp[:8])[0]
                elif skey == "TSMP":
                    strm["tsmp"] = struct.unpack(">L", sp[:4])[0]
                elif skey == "SCAL":
                    info = _FMT.get(stb)
                    if info:
                        unit, fmt = info
                        vals = [struct.unpack(fmt, sp[i * ssz:(i + 1) * ssz])[0]
                                for i in range(srp)]
                        strm["scal"] = vals[0] if len(vals) == 1 else vals
                elif skey in KNOWN_STREAMS:
                    strm["data_key"] = skey
                    n_elems = ssz // (_FMT.get(stb, (1, ""))[0] or 1)
                    rows = []
                    for i in range(srp):
                        row = _decode_row(stb, ssz, n_elems, sp[i * ssz:(i + 1) * ssz])
                        if row is not None:
                            rows.append(row)
                    strm["rows"] = rows
            dk = strm.get("data_key")
            if dk:
                pkt["streams"][dk] = strm
        if pkt["streams"]:
            packets.append(pkt)
    return packets


def extract_timeseries(packets: list, stream_key: str) -> tuple:
    """
    Extract a stream's data with timestamps relative to recording start (seconds).

    Returns (timestamps: list[float], scaled_rows: list[list[float]])
    """
    # First pass: collect all blocks for this stream with their STMP and count
    blocks = []
    for pkt in packets:
        if stream_key not in pkt["streams"]:
            continue
        s = pkt["streams"][stream_key]
        if not s["rows"]:
            continue
        blocks.append(s)

    if not blocks:
        return [], []

    timestamps = []
    scaled_rows = []

    prev_stmp_us = 0
    for b in blocks:
        stmp_us = b["stmp_us"]
        n = len(b["rows"])
        scal = b["scal"]
        if n == 0:
            prev_stmp_us = stmp_us
            continue

        # STMP is the timestamp of the END of this block's samples (µs from recording start)
        # Distribute samples evenly from prev_stmp_us to stmp_us
        dt = (stmp_us - prev_stmp_us) / 1e6  # seconds this block covers
        if dt <= 0:
            dt = n / 200.0  # fallback: assume 200 Hz
        t_start = prev_stmp_us / 1e6
        for i, row in enumerate(b["rows"]):
            t = t_start + (i + 0.5) * dt / n
            timestamps.append(t)
            if isinstance(scal, (int, float)) and scal != 0:
                scaled_rows.append([v / scal for v in row])
            elif isinstance(scal, list):
                scaled_rows.append([v / (scal[j] if j < len(scal) and scal[j] else 1)
                                    for j, v in enumerate(row)])
            else:
                scaled_rows.append(list(row))
        prev_stmp_us = stmp_us

    return timestamps, scaled_rows


# ── File discovery ────────────────────────────────────────────────────────────

def find_missions(root: Path) -> list:
    """Return list of mission folder Paths (those containing MISSION_MARKER)."""
    missions = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / MISSION_MARKER).exists():
            missions.append(entry)
    return missions


def find_gpmf_sources(mission: Path) -> dict:
    """
    Discover MP4 files with GPMF, grouped by camera name.
    Returns {camera: [sorted list of Path objects by chapter]}.

    Strategy: scan both raw/ and main folder; raw/ chapter files take priority
    for any camera where they exist (compacted files lose GPMF on concat).
    """
    raw_dir = mission / "raw"
    raw_cameras: dict = {}     # camera -> {chapter: Path}  from raw/
    main_cameras: dict = {}    # camera -> {chapter: Path}  from main folder

    if raw_dir.is_dir():
        for f in raw_dir.iterdir():
            m = COMPACT_RE.match(f.name)
            if not m or m.group("prefix") == "GL" or m.group("ext").upper() != "MP4":
                continue
            raw_cameras.setdefault(m.group("camera"), {})[int(m.group("chapter"))] = f

    for f in mission.iterdir():
        if not f.is_file():
            continue
        m = COMPACT_RE.match(f.name)
        if m:
            if m.group("prefix") == "GL" or m.group("ext").upper() != "MP4":
                continue
            main_cameras.setdefault(m.group("camera"), {})[int(m.group("chapter"))] = f
        else:
            sm = SIMPLE_RE.match(f.name)
            if sm and sm.group("ext").upper() == "MP4":
                cam = sm.group("camera")
                if not cam.upper().endswith("_LRV"):
                    main_cameras.setdefault(cam, {})[0] = f

    # Merge: raw/ chapters override main folder for the same camera
    merged: dict = {}
    for cam, chapters in main_cameras.items():
        merged[cam] = chapters
    for cam, chapters in raw_cameras.items():
        merged[cam] = chapters   # raw/ wins

    result = {}
    for cam, chapters in merged.items():
        result[cam] = [chapters[k] for k in sorted(chapters)]
    return result


def _has_gpmf_stream(mp4: Path) -> bool:
    """Return True if file has a gpmd stream."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_streams", "-of", "json", str(mp4)],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(out.stdout or "{}")
        return any(s.get("codec_tag_string") == "gpmd" for s in data.get("streams", []))
    except Exception:
        return False


def _gpmf_stream_index(mp4: Path) -> int | None:
    """Return stream index of the gpmd stream, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_streams", "-of", "json", str(mp4)],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(out.stdout or "{}")
        for s in data.get("streams", []):
            if s.get("codec_tag_string") == "gpmd":
                return int(s["index"])
    except Exception:
        pass
    return None


def extract_gpmf_binary(mp4: Path) -> bytes | None:
    """Extract raw GPMF binary from one MP4. Returns None if not found."""
    idx = _gpmf_stream_index(mp4)
    if idx is None:
        return None
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp4), "-map", f"0:{idx}", "-c", "copy",
         "-f", "rawvideo", "/dev/stdout"],
        capture_output=True, timeout=600,
    )
    return result.stdout if result.returncode == 0 and result.stdout else None


# ── Video settings extraction ─────────────────────────────────────────────────

def _find_mp4_box(path: Path, target: str) -> bytes | None:
    """Return payload bytes of the first top-level MP4 box matching target fourcc."""
    try:
        with open(path, "rb") as f:
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None
                size = struct.unpack(">I", hdr[:4])[0]
                fourcc = hdr[4:8].decode("ascii", errors="replace")
                if size == 0:
                    # Box extends to EOF
                    return f.read() if fourcc == target else None
                if size == 1:
                    # 64-bit extended size follows the fourcc
                    ext = f.read(8)
                    if len(ext) < 8:
                        return None
                    size = struct.unpack(">Q", ext)[0]
                    payload_skip = size - 16
                else:
                    payload_skip = size - 8
                if payload_skip < 0:
                    return None
                if fourcc == target:
                    return f.read(payload_skip)
                f.seek(payload_skip, 1)
    except Exception:
        return None


def _walk_boxes(data: bytes) -> dict:
    """Return {fourcc: payload_bytes} for immediate children of a container box."""
    buf = io.BytesIO(data)
    boxes: dict = {}
    while buf.tell() < len(data) - 8:
        hdr = buf.read(8)
        if len(hdr) < 8:
            break
        size = struct.unpack(">I", hdr[:4])[0]
        fourcc = hdr[4:8].decode("ascii", errors="replace")
        if size < 8:
            break
        boxes[fourcc] = buf.read(max(0, size - 8))
    return boxes


def _decode_klv_val(tb: int, sz: int, rp: int, payload: bytes):
    """Decode a single GPMF KLV scalar/string. Returns None for unsupported types."""
    if tb in (ord('c'), ord('U')):
        return payload[:sz * rp].decode("ascii", errors="replace").rstrip("\x00")
    if tb == ord('F'):
        return payload[:4].decode("ascii", errors="replace").rstrip("\x00")
    if tb == ord('L') and sz == 4:
        vals = [struct.unpack(">I", payload[i*4:i*4+4])[0] for i in range(rp)]
        return vals[0] if rp == 1 else vals
    if tb == ord('l') and sz == 4:
        vals = [struct.unpack(">i", payload[i*4:i*4+4])[0] for i in range(rp)]
        return vals[0] if rp == 1 else vals
    if tb == ord('s') and sz == 2:
        vals = [struct.unpack(">h", payload[i*2:i*2+2])[0] for i in range(rp)]
        return vals[0] if rp == 1 else vals
    if tb == ord('S') and sz == 2:
        vals = [struct.unpack(">H", payload[i*2:i*2+2])[0] for i in range(rp)]
        return vals[0] if rp == 1 else vals
    if tb == ord('f') and sz == 4:
        vals = [struct.unpack(">f", payload[i*4:i*4+4])[0] for i in range(rp)]
        return vals[0] if rp == 1 else vals
    if tb == ord('J') and sz == 8:
        return struct.unpack(">Q", payload[:8])[0]
    if tb in (ord('B'), ord('b')) and sz == 1:
        return payload[0] if rp == 1 else list(payload[:rp])
    return None


def _parse_udta_gpmf(raw: bytes) -> dict:
    """
    Parse the udta/GPMF settings blob embedded in the MP4 moov box.
    Returns {'settings': {key: val, ...}, 'fov': {'name': str, key: val, ...}}.
    """
    gs: dict = {}
    fov: dict = {}
    for key, tb, sz, rp, payload in _parse_klvs(raw):
        if key != "DEVC":
            continue
        dvnm = ""
        dvid_str = ""
        for dkey, dtb, dsz, drp, dpayload in _parse_klvs(payload):
            if dkey == "DVNM":
                dvnm = dpayload.decode("ascii", errors="replace").rstrip("\x00")
            elif dkey == "DVID":
                dvid_str = dpayload[:4].decode("ascii", errors="replace")

        is_global = dvnm == "Global Settings"
        is_fov = dvid_str == "FOVL" or "FOV" in dvnm
        if not (is_global or is_fov):
            continue

        target = gs if is_global else fov
        if is_fov:
            fov["name"] = dvnm
        for dkey, dtb, dsz, drp, dpayload in _parse_klvs(payload):
            if dkey in ("DVNM", "DVID"):
                continue
            val = _decode_klv_val(dtb, dsz, drp, dpayload)
            if val is not None:
                target[dkey] = val
    return {"settings": gs, "fov": fov}


def collect_video_settings(mp4: Path) -> dict:
    """
    Extract camera and recording settings from one MP4 file.

    Sources:
    - ffprobe video stream: codec, resolution, frame rate, color space, bitrate
    - ffprobe format: container-level firmware tag, total bitrate
    - MP4 udta/GPMF Global Settings block: lens mode, HyperSmooth, ProTune
      (color profile, sharpness, white balance, ISO limits, EV comp), firmware,
      model name, serial number, orientation, bitrate mode
    - MP4 udta/GPMF FOV block: human-readable FOV name, horizontal FOV degrees
    """
    result: dict = {}

    # ffprobe: video stream + container tags
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_streams", "-show_format", "-of", "json", str(mp4)],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(out.stdout or "{}")
        for stream in data.get("streams", []):
            if stream.get("codec_type") != "video":
                continue
            fps_str = stream.get("r_frame_rate", "")
            fps = None
            if "/" in fps_str:
                n, d = fps_str.split("/")
                fps = round(int(n) / int(d), 4) if int(d) else None
            result.update({
                "codec":               stream.get("codec_name"),
                "codec_profile":       stream.get("profile"),
                "width":               stream.get("width"),
                "height":              stream.get("height"),
                "aspect_ratio":        stream.get("display_aspect_ratio"),
                "frame_rate":          fps,
                "frame_rate_rational": fps_str,
                "video_bitrate_bps":   int(stream.get("bit_rate") or 0) or None,
                "color_space":         stream.get("color_space"),
                "color_transfer":      stream.get("color_transfer"),
                "color_primaries":     stream.get("color_primaries"),
                "color_range":         stream.get("color_range"),
                "pix_fmt":             stream.get("pix_fmt"),
                "timecode":            stream.get("tags", {}).get("timecode"),
            })
            break
        fmt = data.get("format", {})
        result["total_bitrate_bps"] = int(fmt.get("bit_rate") or 0) or None
        result["firmware"] = fmt.get("tags", {}).get("firmware")
    except Exception:
        pass

    # udta/GPMF settings block embedded in the MP4 container
    try:
        moov = _find_mp4_box(mp4, "moov")
        if moov:
            gpmf_raw = _walk_boxes(_walk_boxes(moov).get("udta", b"")).get("GPMF")
            if gpmf_raw:
                parsed = _parse_udta_gpmf(gpmf_raw)
                gs = parsed["settings"]
                fov = parsed["fov"]

                # Firmware / device identity (GPMF value is more specific than format tag)
                if gs.get("FMWR"):
                    result["firmware"] = gs["FMWR"]
                result["model"]         = gs.get("MINF")
                result["serial_number"] = gs.get("CASN")
                result["lens_info"]     = gs.get("LINF")

                # Lens / projection type
                prjt = gs.get("PRJT") or ""
                result["lens_mode_code"] = prjt or None
                result["lens_mode"]      = _PRJT_LENS_MODES.get(prjt, prjt or None)

                # Electronic Image Stabilization / HyperSmooth
                result["hypersmooth_enabled"] = (gs.get("EISE") == "Y")
                result["hypersmooth_setting"] = gs.get("HSGT")   # OFF / ON / BOOST / AUTO_BOOST
                result["hypersmooth_level"]   = gs.get("HCTL")   # Off / On / Boost (display)

                # ProTune settings
                result["protune"]         = (gs.get("PRTN") == "Y")
                result["color_profile"]   = gs.get("PTCL")   # NATURAL / FLAT / GoPro / HLG
                result["sharpness"]       = gs.get("PTSH")   # LOW / MED / HIGH
                result["white_balance"]   = gs.get("PTWB")   # AUTO / 2300K / … / 6500K
                result["iso_min"]         = gs.get("PIMN")
                result["iso_max"]         = gs.get("PIMX")
                result["iso_mode"]        = gs.get("PIMD")   # AUTO / MANUAL
                result["ev_compensation"] = gs.get("PTEV")
                result["exposure_type"]   = gs.get("EXPT")   # AUTO / MANUAL

                # Camera state
                result["orientation"]  = gs.get("OREN")   # U=up D=down L=left R=right
                result["bitrate_mode"] = gs.get("BITR")   # STANDARD / HIGH

                # FOV block (separate DEVC with ZFOV float and human name)
                if fov:
                    result["fov_name"] = fov.get("name")
                    zfov = fov.get("ZFOV")
                    result["fov_horizontal_deg"] = round(zfov, 2) if zfov is not None else None
    except Exception:
        pass

    return result


# ── Metadata ──────────────────────────────────────────────────────────────────

def collect_file_meta(mp4: Path) -> dict:
    """Collect size, duration, creation_time for a single MP4."""
    size = mp4.stat().st_size if mp4.exists() else 0
    ct, dur = ffprobe(str(mp4))
    return {
        "path": str(mp4),
        "size_bytes": size,
        "size_mb": round(size / 1024 / 1024, 1),
        "duration_s": round(dur, 2) if dur else None,
        "creation_time_utc": (
            datetime.fromtimestamp(ct, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if ct else None
        ),
        "creation_time_epoch": ct,
    }


# ── CSV / GPX export ──────────────────────────────────────────────────────────

def write_csv(path: Path, header: list, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def write_gpx(path: Path, gps_rows: list, timestamps: list, camera: str) -> None:
    """Write a GPS track to a GPX file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="extract_telemetry.py"',
        '     xmlns="http://www.topografix.com/GPX/1/1">',
        f'  <trk><name>{camera}</name><trkseg>',
    ]
    for t, row in zip(timestamps, gps_rows):
        if len(row) < 2:
            continue
        lat, lon = row[0], row[1]
        alt = row[2] if len(row) > 2 else 0.0
        ts_str = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"><ele>{alt:.2f}</ele><time>{ts_str}</time></trkpt>')
    lines += ["  </trkseg></trk>", "</gpx>"]
    path.write_text("\n".join(lines))


# ── Plotting ──────────────────────────────────────────────────────────────────

def _magnitude(rows):
    return [sum(v ** 2 for v in row) ** 0.5 for row in rows]


def plot_stream(ts, rows, cols, title, ylabel, out_path: Path, color_cycle=None) -> None:
    """Plot a multi-axis time series to a PNG."""
    fig, ax = plt.subplots(figsize=(14, 4))
    colors = color_cycle or ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]
    for i, col in enumerate(cols):
        vals = [row[i] for row in rows if i < len(row)]
        ax.plot(ts[:len(vals)], vals, color=colors[i % len(colors)],
                linewidth=0.6, label=col, alpha=0.85)
    mag = _magnitude(rows)
    ax.plot(ts[:len(mag)], mag, color="black", linewidth=0.8,
            linestyle="--", label="magnitude", alpha=0.5)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_gps_track(gps_data: dict, out_path: Path, mission_name: str) -> None:
    """
    Plot GPS tracks for all cameras on one figure.
    gps_data: {camera: (timestamps, rows)}
    """
    fig, axes = plt.subplots(1, 2, figsize=(16, 8))
    ax_map, ax_alt = axes

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    for i, (cam, (ts, rows)) in enumerate(gps_data.items()):
        if not rows:
            continue
        lats = [r[0] for r in rows]
        lons = [r[1] for r in rows]
        alts = [r[2] for r in rows if len(r) > 2]
        c = colors[i % len(colors)]
        # Track: color-code by time
        sc = ax_map.scatter(lons, lats, c=ts[:len(lats)], cmap="plasma",
                            s=1, alpha=0.7, label=cam)
        ax_map.plot(lons[0], lats[0], "^", color=c, markersize=8)
        ax_map.plot(lons[-1], lats[-1], "s", color=c, markersize=8)
        if alts:
            ax_alt.plot(ts[:len(alts)], alts, color=c, linewidth=1.0, label=cam)

    ax_map.set_xlabel("Longitude")
    ax_map.set_ylabel("Latitude")
    ax_map.set_title(f"{mission_name} — GPS Track (color=time)")
    ax_map.set_aspect("equal")
    ax_map.legend(fontsize=8)
    ax_map.grid(True, alpha=0.3)

    ax_alt.set_xlabel("Time (s)")
    ax_alt.set_ylabel("Altitude (m)")
    ax_alt.set_title("Altitude over time")
    ax_alt.legend(fontsize=8)
    ax_alt.grid(True, alpha=0.3)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_accel_comparison(accel_data: dict, out_path: Path, mission_name: str) -> None:
    """Overlay accelerometer magnitudes for all cameras."""
    fig, ax = plt.subplots(figsize=(16, 5))
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    for i, (cam, (ts, rows)) in enumerate(accel_data.items()):
        if not rows:
            continue
        mag = _magnitude(rows)
        ax.plot(ts[:len(mag)], mag, color=colors[i % len(colors)],
                linewidth=0.6, label=cam, alpha=0.8)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Acceleration magnitude (m/s²)")
    ax.set_title(f"{mission_name} — All cameras: accelerometer magnitude")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_summary(mission: Path, meta: dict, stream_summaries: dict, out_path: Path) -> None:
    """
    Text + sparkline summary figure: file sizes, durations, sync offsets.
    stream_summaries: {camera: {stream_key: n_samples}}
    """
    fig = plt.figure(figsize=(14, 8))
    ax = fig.add_subplot(111)
    ax.axis("off")

    cameras = list(meta.get("cameras", {}).keys())
    lines = [f"  Mission: {mission.name}", ""]
    ref_epoch = None
    for cam, info in meta.get("cameras", {}).items():
        if ref_epoch is None and info.get("creation_time_epoch"):
            ref_epoch = info["creation_time_epoch"]

    for cam in cameras:
        info = meta["cameras"][cam]
        dur = f"{info.get('total_duration_s', 0):.1f}s" if info.get("total_duration_s") else "?"
        sz = f"{info.get('total_size_mb', 0):.0f} MB" if info.get("total_size_mb") else "?"
        ct = info.get("creation_time_utc", "?")
        has_gps = "GPS" if info.get("has_gps") else "no GPS"
        n_ch = info.get("n_chapters", 1)
        streams = ", ".join(stream_summaries.get(cam, {}).keys()) or "(none)"
        offset_s = ""
        if ref_epoch and info.get("creation_time_epoch"):
            off = info["creation_time_epoch"] - ref_epoch
            offset_s = f"  sync_offset={off:+.2f}s"
        lines.append(f"  {cam}:")
        lines.append(f"    {n_ch} chapter(s)  |  {dur}  |  {sz}  |  {has_gps}{offset_s}")
        lines.append(f"    start: {ct}")
        lines.append(f"    streams: {streams}")
        for sk, n in stream_summaries.get(cam, {}).items():
            lines.append(f"      {sk}: {n:,} samples")
        lines.append("")

    text = "\n".join(lines)
    ax.text(0.02, 0.98, text, transform=ax.transAxes,
            fontsize=9, verticalalignment="top", fontfamily="monospace",
            bbox=dict(boxstyle="round", facecolor="lightyellow", alpha=0.5))
    ax.set_title(f"{mission.name} — Telemetry Summary", fontsize=12, fontweight="bold")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


# ── Per-mission processing ────────────────────────────────────────────────────

def process_mission(mission: Path, dry_run: bool, force: bool, no_plots: bool,
                    verbose: bool) -> None:
    print(f"\n{'='*60}")
    print(f"Mission: {mission.name}  ({mission})")

    sources = find_gpmf_sources(mission)
    if not sources:
        print("  No MP4 sources found — skipping.")
        return

    data_dir = mission / "data"
    plots_dir = data_dir / "plots"

    if data_dir.exists() and not force:
        existing = list(data_dir.glob("*.csv"))
        if existing:
            print(f"  data/ already has {len(existing)} CSV(s). Use --force to re-extract.")
            return

    print(f"  Cameras: {', '.join(sorted(sources))}")

    if dry_run:
        for cam, paths in sorted(sources.items()):
            print(f"  [dry-run] {cam}: {len(paths)} chapter(s)")
            for p in paths:
                sz = p.stat().st_size / 1024 / 1024
                print(f"    {p.name}  ({sz:.1f} MB)")
        return

    # ── Per-camera extraction ──────────────────────────────────────────────
    camera_meta = {}
    camera_stream_data: dict = {}   # {cam: {stream_key: (ts, rows)}}
    stream_summaries: dict = {}     # {cam: {stream_key: n_samples}}
    gps_data: dict = {}
    accel_data: dict = {}

    for cam, paths in sorted(sources.items()):
        print(f"\n  [{cam}]  {len(paths)} chapter(s)")

        # Metadata per camera
        chapter_metas = [collect_file_meta(p) for p in paths]
        total_size = sum(m["size_bytes"] for m in chapter_metas)
        total_dur = sum(m["duration_s"] or 0 for m in chapter_metas)
        first_ct = chapter_metas[0].get("creation_time_epoch")
        first_ct_str = chapter_metas[0].get("creation_time_utc")
        print(f"    reading video settings: {paths[0].name} ...", end=" ", flush=True)
        vid_settings = collect_video_settings(paths[0])
        print(f"{vid_settings.get('codec','?').upper()} {vid_settings.get('width')}x{vid_settings.get('height')} "
              f"@ {vid_settings.get('frame_rate','?')} fps  lens={vid_settings.get('lens_mode','?')}")
        camera_meta[cam] = {
            "source_files": [str(p) for p in paths],
            "n_chapters": len(paths),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / 1024 / 1024, 1),
            "total_duration_s": round(total_dur, 2) if total_dur else None,
            "creation_time_utc": first_ct_str,
            "creation_time_epoch": first_ct,
            "has_gps": False,
            "streams": {},
            "video_settings": vid_settings,
        }

        # Extract and concatenate GPMF from all chapters
        all_packets: list = []
        chapter_offset_us = 0
        for p in paths:
            print(f"    extracting GPMF: {p.name} ...", end=" ", flush=True)
            raw = extract_gpmf_binary(p)
            if raw is None:
                print("no GPMF stream")
                continue
            pkts = parse_gpmf_packets(raw)
            # Offset chapter timestamps so they are continuous
            if chapter_offset_us > 0:
                for pkt in pkts:
                    for s in pkt["streams"].values():
                        s["stmp_us"] += chapter_offset_us
            # Compute offset for next chapter: last STMP of this chapter
            for pkt in reversed(pkts):
                for s in pkt["streams"].values():
                    chapter_offset_us = max(chapter_offset_us, s["stmp_us"])
                break
            all_packets.extend(pkts)
            print(f"{len(pkts)} packets")

        if not all_packets:
            print("    No GPMF data found.")
            continue

        # Extract each stream
        cam_streams = {}
        camera_stream_data[cam] = {}
        stream_summaries[cam] = {}

        for stream_key, (human_name, col_names) in KNOWN_STREAMS.items():
            ts, rows = extract_timeseries(all_packets, stream_key)
            if not ts:
                continue

            # Exposure telemetry: average into video_settings, no CSV
            if stream_key in METADATA_ONLY_STREAMS:
                vals = [row[0] for row in rows if row]
                if vals:
                    stat_key = {"SHUT": "avg_shutter_s", "ISOE": "avg_iso", "TMPC": "avg_temp_c"}[stream_key]
                    camera_meta[cam]["video_settings"][stat_key] = round(sum(vals) / len(vals), 4)
                continue

            camera_meta[cam]["streams"][stream_key] = len(ts)
            stream_summaries[cam][stream_key] = len(ts)
            camera_stream_data[cam][stream_key] = (ts, rows)

            if stream_key == "GPS5":
                camera_meta[cam]["has_gps"] = True
                gps_data[cam] = (ts, rows)
                # Absolute timestamps for GPX
                if first_ct:
                    abs_ts = [first_ct + t for t in ts]
                    gpx_path = data_dir / f"{cam}.gpx"
                    write_gpx(gpx_path, rows, abs_ts, cam)
                    print(f"    GPS: {len(ts)} points → {gpx_path.name}")
                # GPS CSV: absolute timestamps if available
                gps_rows = []
                for t, row in zip(ts, rows):
                    abs_t = (first_ct + t) if first_ct else t
                    gps_rows.append([round(abs_t, 3)] + [round(v, 7) for v in row])
                write_csv(data_dir / f"{cam}_gps.csv",
                          ["timestamp_epoch"] + col_names, gps_rows)

            elif stream_key in ("ACCL", "GYRO", "GRAV", "CORI", "IORI"):
                fname = f"{cam}_{stream_key.lower()}.csv"
                csv_rows = [[round(t, 4)] + [round(v, 6) for v in row]
                            for t, row in zip(ts, rows)]
                write_csv(data_dir / fname, ["t_s"] + col_names, csv_rows)
                print(f"    {stream_key}: {len(ts):,} samples → {fname}")

                if stream_key == "ACCL":
                    accel_data[cam] = (ts, rows)

        cam_streams = camera_stream_data.get(cam, {})

    # ── Metadata JSON ──────────────────────────────────────────────────────
    # Compute sync offsets relative to the earliest camera
    epochs = {cam: info.get("creation_time_epoch") for cam, info in camera_meta.items()
               if info.get("creation_time_epoch")}
    ref_epoch = min(epochs.values()) if epochs else None
    sync_offsets = {cam: round(e - ref_epoch, 4) for cam, e in epochs.items()} if ref_epoch else {}

    meta = {
        "mission": mission.name,
        "cameras": camera_meta,
        "sync_offsets_s": sync_offsets,
    }
    meta_path = data_dir / "metadata.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"\n  Metadata → {meta_path}")

    # ── Plots ──────────────────────────────────────────────────────────────
    if not HAS_MPL or no_plots:
        if not HAS_MPL:
            print("  Skipping plots (matplotlib not available).")
        return

    print("  Generating plots ...")

    if gps_data:
        plot_gps_track(gps_data, plots_dir / "gps_all_cameras.png", mission.name)
        print(f"    → plots/gps_all_cameras.png")

    if accel_data:
        plot_accel_comparison(accel_data, plots_dir / "all_cameras_accel_magnitude.png",
                               mission.name)
        print(f"    → plots/all_cameras_accel_magnitude.png")

    for cam, cam_data in camera_stream_data.items():
        for stream_key, (ts, rows) in cam_data.items():
            if stream_key not in ("ACCL", "GYRO", "GRAV"):
                continue
            _, col_names = KNOWN_STREAMS[stream_key]
            units = {"ACCL": "m/s²", "GYRO": "rad/s", "GRAV": "normalized"}.get(stream_key, "")
            fname = f"{cam}_{stream_key.lower()}.png"
            plot_stream(
                ts, rows, col_names,
                title=f"{mission.name} — {cam} {stream_key}",
                ylabel=f"{stream_key} ({units})",
                out_path=plots_dir / fname,
            )
            print(f"    → plots/{fname}")

    plot_summary(mission, meta, stream_summaries, plots_dir / "summary.png")
    print(f"    → plots/summary.png")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="Mission root folder or single mission folder")
    ap.add_argument("--mission", help="Process only this mission name")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be extracted without doing it")
    ap.add_argument("--force", action="store_true",
                    help="Re-extract even if data/ already exists")
    ap.add_argument("--no-plots", action="store_true", help="Skip plot generation")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")

    # Single-mission mode: if root itself has the marker
    if (root / MISSION_MARKER).exists():
        missions = [root]
    else:
        missions = find_missions(root)
        if not missions:
            # Maybe root itself is the only mission passed without marker
            sys.exit(f"No missions found in {root}\n"
                     "(Expected subdirectories containing .gopro_mission marker)")

    if args.mission:
        missions = [m for m in missions if m.name == args.mission]
        if not missions:
            sys.exit(f"Mission '{args.mission}' not found under {root}")

    print(f"Found {len(missions)} mission(s): {', '.join(m.name for m in missions)}")

    for m in missions:
        process_mission(m, dry_run=args.dry_run, force=args.force,
                        no_plots=args.no_plots, verbose=args.verbose)

    print("\nDone.")


if __name__ == "__main__":
    main()
