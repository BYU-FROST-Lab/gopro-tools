#!/usr/bin/env python3
"""
sync_gyro.py — estimate temporal offsets between GoPro cameras via gyroscope magnitude.

Since cameras are rigidly mounted, |ω| = √(gx²+gy²+gz²) is rotation-invariant:
the same in every coordinate frame.  Cross-correlating |ω| time-series finds clock
offsets without needing to know the inter-camera rotation.

Gyro offset convention (same as sync_offsets_s in metadata.json):
  gyro_offsets_s[C] = seconds camera C started AFTER the reference camera.
  Negative  → C started BEFORE the reference camera.
  Zero      → C is the reference camera.
  Positive  → C started AFTER the reference camera.

The reference camera is whichever camera has sync_offsets_s = 0.0 in metadata.json.
Gyro results are written back into that same metadata.json as "gyro_offsets_s".

Resolution: ±5 ms native at 200 Hz; parabolic sub-sample interpolation typically
achieves ±1–2 ms given sharp, impulsive motions.

Usage:
  python sync_gyro.py ROOT [--max-lag S] [--dt S] [--plot] [--dry-run] [--force]

  ROOT may be a single mission folder or a parent folder containing multiple missions.
  Missions are identified by the presence of a .gopro_mission marker file.
"""

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

from utils import MISSION_MARKER, find_missions, load_metadata, save_metadata

MIN_DURATION_S = 5.0   # skip cameras with less than this much gyro data
MIN_OVERLAP_S  = 3.0   # skip pairs with less than this much estimated overlap


def reference_camera(meta: dict) -> str | None:
    """Return the camera with sync_offsets_s = 0.0, or None if not available."""
    offsets = meta.get("sync_offsets_s", {})
    for cam, v in offsets.items():
        if v == 0.0:
            return cam
    # Fall back to minimum-epoch camera
    epochs = {c: info.get("creation_time_epoch")
              for c, info in meta.get("cameras", {}).items()
              if info.get("creation_time_epoch") is not None}
    if epochs:
        return min(epochs, key=epochs.__getitem__)
    return None


# ── data loading ──────────────────────────────────────────────────────────────

def load_gyro(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (t_s, |ω|_rad_s) from a _gyro.csv produced by extract_telemetry.py."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append((float(row["t_s"]),
                         float(row["gx_rads"]),
                         float(row["gy_rads"]),
                         float(row["gz_rads"])))
    if not rows:
        raise ValueError("empty gyro CSV")
    arr = np.array(rows, dtype=float)
    t   = arr[:, 0]
    mag = np.sqrt(arr[:, 1] ** 2 + arr[:, 2] ** 2 + arr[:, 3] ** 2)
    return t, mag


def find_gyros(mission: Path) -> dict[str, Path]:
    """Return {camera: path} for all *_gyro.csv files in MISSION/data/."""
    data_dir = mission / "data"
    if not data_dir.exists():
        return {}
    return {p.stem.removesuffix("_gyro"): p
            for p in sorted(data_dir.glob("*_gyro.csv"))}


# ── signal processing ─────────────────────────────────────────────────────────

def _norm(v: np.ndarray) -> np.ndarray:
    return (v - v.mean()) / (v.std() + 1e-12)


def _uniform(t: np.ndarray, y: np.ndarray, dt: float) -> np.ndarray:
    return np.interp(np.arange(t[0], t[-1], dt), t, y)


def _xcorr(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Full linear cross-correlation C[L] = Σ_n a[n] · b[n − L].
    Returns (corr, lags) with lags ∈ [−(N−1), M−1] where M=len(a), N=len(b).

    Peak at L=L₀ means b started L₀·dt seconds AFTER a (positive L₀ = b started later).
    To place b on a's timeline: b_aligned_time = b_time + L₀·dt.
    """
    M, N = len(a), len(b)
    n_full = M + N - 1
    nfft = 1 << (n_full - 1).bit_length()  # smallest power-of-2 ≥ n_full

    R = np.fft.irfft(
        np.fft.rfft(a, n=nfft) * np.conj(np.fft.rfft(b, n=nfft)),
        n=nfft,
    )
    # R[0..M-1] = lags 0..M-1;  R[nfft-N+1..] = lags -(N-1)..-1
    corr = np.empty(n_full)
    if N > 1:
        corr[: N - 1] = R[nfft - N + 1 :]
    corr[N - 1 :] = R[:M]
    lags = np.arange(-(N - 1), M)
    return corr, lags


def find_offset(
    t_ref: np.ndarray, mag_ref: np.ndarray,
    t_other: np.ndarray, mag_other: np.ndarray,
    dt: float, max_lag: float,
) -> tuple[float, float]:
    """
    Return (offset_s, peak_r) where offset_s is how many seconds OTHER started
    AFTER ref (negative = other started before ref).  Same sign convention as
    sync_offsets_s in metadata.json.
    peak_r: normalised cross-correlation peak; below ~0.3 is low confidence.
    """
    dur_ref   = t_ref[-1]   - t_ref[0]
    dur_other = t_other[-1] - t_other[0]
    if dur_ref < MIN_DURATION_S:
        raise ValueError(f"reference recording too short ({dur_ref:.1f} s < {MIN_DURATION_S} s)")
    if dur_other < MIN_DURATION_S:
        raise ValueError(f"camera recording too short ({dur_other:.1f} s < {MIN_DURATION_S} s)")

    y_ref   = _norm(_uniform(t_ref,   mag_ref,   dt))
    y_other = _norm(_uniform(t_other, mag_other, dt))

    corr, lags = _xcorr(y_ref, y_other)

    mask = np.abs(lags) <= int(round(max_lag / dt))
    c_w, l_w = corr[mask], lags[mask]

    if len(c_w) == 0:
        raise ValueError(f"max_lag={max_lag} s leaves no lags — increase --max-lag")

    pk = int(np.argmax(c_w))
    peak_r = c_w[pk] / min(len(y_ref), len(y_other))

    # Parabolic sub-sample refinement
    if 0 < pk < len(c_w) - 1:
        a, b, c = c_w[pk - 1], c_w[pk], c_w[pk + 1]
        delta = 0.5 * (a - c) / (a - 2 * b + c + 1e-30)
    else:
        delta = 0.0

    if pk == 0 or pk == len(c_w) - 1:
        print(
            f"    warning: peak at edge of ±{max_lag} s window — try --max-lag",
            file=sys.stderr,
        )

    # Positive lag L₀ means other started L₀·dt seconds AFTER ref.
    # Same sign convention as sync_offsets_s: positive = started later, negative = started earlier.
    offset_s = (l_w[pk] + delta) * dt
    return float(offset_s), float(peak_r)


# ── per-mission processing ────────────────────────────────────────────────────

def process_mission(
    mission: Path,
    dt: float,
    max_lag: float,
    dry_run: bool,
    force: bool,
    do_plot: bool,
) -> None:
    name = mission.name
    meta = load_metadata(mission)

    if not meta:
        print(f"  {name}: no metadata.json — skipping")
        return

    if "gyro_offsets_s" in meta and not force:
        print(f"  {name}: gyro_offsets_s already present (use --force to re-run)")
        return

    gyros = find_gyros(mission)
    if len(gyros) < 2:
        print(f"  {name}: fewer than 2 cameras with gyro data — skipping")
        return

    ref = reference_camera(meta)
    if ref is None:
        ref = sorted(gyros)[0]
        print(f"  {name}: no reference in metadata, using '{ref}'")
    elif ref not in gyros:
        print(f"  {name}: reference camera '{ref}' has no gyro CSV — skipping")
        return

    cameras = sorted(gyros)
    print(f"\n{'─' * 60}")
    print(f"  {name}  |  reference: {ref}")
    print(f"{'─' * 60}")

    # Load gyro signals
    gyro_data: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for cam in cameras:
        try:
            t, mag = load_gyro(gyros[cam])
        except Exception as e:
            print(f"    {cam}: failed to load gyro — {e}")
            continue
        gyro_data[cam] = (t, mag)
        dur = t[-1] - t[0]
        hz  = 1.0 / float(np.median(np.diff(t)))
        print(f"    {cam:<16s}  {len(t):6d} samples  {dur:.1f} s  ({hz:.0f} Hz)")

    if ref not in gyro_data:
        print(f"    reference '{ref}' failed to load — skipping mission")
        return

    t_ref, mag_ref = gyro_data[ref]

    # Compute gyro offsets (seconds after ref; negative = started before ref)
    gyro_offsets: dict[str, float | None] = {ref: 0.0}
    peak_rs: dict[str, float | None]      = {ref: None}

    print()
    for cam in cameras:
        if cam == ref or cam not in gyro_data:
            continue
        t_o, mag_o = gyro_data[cam]
        try:
            off, r = find_offset(t_ref, mag_ref, t_o, mag_o, dt=dt, max_lag=max_lag)
        except ValueError as e:
            print(f"    {cam:<16s}  ERROR — {e}")
            gyro_offsets[cam] = None
            peak_rs[cam]      = None
            continue
        quality = "GOOD" if r > 0.5 else ("FAIR" if r > 0.3 else "LOW ")
        print(f"    {cam:<16s}  gyro offset = {off * 1000:+9.2f} ms   r={r:.4f}  [{quality}]")
        gyro_offsets[cam] = round(off, 6)
        peak_rs[cam]      = round(r, 6)

    # Pairwise consistency check (≥3 cameras)
    non_ref = [c for c in cameras if c != ref and c in gyro_data
               and gyro_offsets.get(c) is not None]
    if len(non_ref) >= 2:
        print("\n    Pairwise consistency:")
        for i, c1 in enumerate(non_ref):
            for c2 in non_ref[i + 1:]:
                t1, m1 = gyro_data[c1]
                t2, m2 = gyro_data[c2]
                try:
                    off12, _ = find_offset(t1, m1, t2, m2, dt=dt, max_lag=max_lag)
                except Exception:
                    continue
                expected = gyro_offsets[c2] - gyro_offsets[c1]
                residual = off12 - expected
                print(
                    f"      {c1} vs {c2}:  direct={off12*1000:+.1f} ms  "
                    f"via-ref={expected*1000:+.1f} ms  "
                    f"residual={residual*1000:+.1f} ms"
                )

    # Comparison table: gyro vs timestamp offsets
    ts_offsets = meta.get("sync_offsets_s", {})
    if ts_offsets:
        print(f"\n    {'Camera':<16s}  {'gyro (ms)':>12s}  {'timestamp (ms)':>14s}  {'delta (ms)':>10s}")
        print(f"    {'------':<16s}  {'----------':>12s}  {'----------':>14s}  {'----------':>10s}")
        for cam in cameras:
            g = gyro_offsets.get(cam)
            t = ts_offsets.get(cam)
            g_str = f"{g * 1000:+.1f}" if g is not None else "   —"
            t_str = f"{t * 1000:+.1f}" if t is not None else "   —"
            if g is not None and t is not None:
                d_str = f"{(g - t) * 1000:+.1f}"
            else:
                d_str = "   —"
            print(f"    {cam:<16s}  {g_str:>12s}  {t_str:>14s}  {d_str:>10s}")

    # Write back to metadata.json
    if not dry_run:
        meta["gyro_offsets_s"]      = gyro_offsets
        meta["gyro_offsets_peak_r"] = peak_rs
        save_metadata(mission, meta)
        print(f"\n    → gyro_offsets_s written to {mission / 'data' / 'metadata.json'}")
    else:
        print("\n    (dry-run: metadata.json not modified)")

    if do_plot:
        _plot(cameras, ref, gyro_data, gyro_offsets, dt, max_lag, name)


# ── plotting ──────────────────────────────────────────────────────────────────

def _plot(
    cameras: list[str],
    ref: str,
    gyro_data: dict[str, tuple[np.ndarray, np.ndarray]],
    gyro_offsets: dict[str, float | None],
    dt: float,
    max_lag: float,
    title: str,
) -> None:
    try:
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
    except ImportError:
        print("matplotlib not installed — skipping --plot", file=sys.stderr)
        return

    non_ref = [c for c in cameras if c != ref and c in gyro_data]
    nrows = 1 + len(non_ref)

    fig = plt.figure(figsize=(14, 3.5 * nrows))
    fig.suptitle(f"{title}  |  reference: {ref}", fontsize=12, y=0.99)
    gs = gridspec.GridSpec(nrows, 2, figure=fig, hspace=0.55, wspace=0.35)

    t_ref, mag_ref = gyro_data[ref]

    # Row 0: all cameras on a common timeline
    ax0 = fig.add_subplot(gs[0, :])
    for cam in cameras:
        if cam not in gyro_data:
            continue
        t, mag = gyro_data[cam]
        off = gyro_offsets.get(cam) or 0.0
        # t_ref = t_cam + off; place cam's events on ref's timeline
        ax0.plot(t + off, mag, lw=0.4, alpha=0.75, label=cam)
    ax0.set_xlabel("reference timeline (s)")
    ax0.set_ylabel("|ω| (rad/s)")
    ax0.set_title("All cameras aligned to reference timeline")
    ax0.legend(fontsize=8)

    y_ref_grid = _norm(_uniform(t_ref, mag_ref, dt))

    for row, cam in enumerate(non_ref, start=1):
        t_o, mag_o = gyro_data[cam]
        y_o_grid = _norm(_uniform(t_o, mag_o, dt))

        corr, lags = _xcorr(y_ref_grid, y_o_grid)
        lag_s = lags * dt

        off = gyro_offsets.get(cam) or 0.0
        # peak lag = off/dt seconds (positive off = cam started after ref = positive lag)
        lag_peak_s = off

        # Left: cross-correlation
        ax_cc = fig.add_subplot(gs[row, 0])
        win = np.abs(lag_s) <= min(max_lag * 1.1, np.abs(lag_s).max())
        norm_corr = corr / min(len(y_ref_grid), len(y_o_grid))
        ax_cc.plot(lag_s[win], norm_corr[win], lw=0.8, color="C0")
        ax_cc.axvline(lag_peak_s, color="r", lw=1.5,
                      label=f"peak → {off * 1000:+.1f} ms after ref")
        ax_cc.set_xlabel("lag (s)")
        ax_cc.set_ylabel("normalised xcorr")
        ax_cc.set_title(f"cross-correlation  {ref} × {cam}")
        ax_cc.legend(fontsize=8)

        # Right: aligned signals
        ax_al = fig.add_subplot(gs[row, 1])
        t_start = max(t_ref[0], t_o[0] + off)
        t_end   = min(t_ref[-1], t_o[-1] + off)
        if t_end > t_start + 1.0:
            tg = np.arange(t_start, t_end, dt)
            y1 = np.interp(tg, t_ref, mag_ref)
            y2 = np.interp(tg - off, t_o, mag_o)
            ax_al.plot(tg - tg[0], y1, lw=0.4, alpha=0.8, label=ref)
            ax_al.plot(tg - tg[0], y2, lw=0.4, alpha=0.8, label=f"{cam} (aligned)")
        ax_al.set_xlabel("time (s)")
        ax_al.set_ylabel("|ω| (rad/s)")
        ax_al.set_title(f"aligned signals  {ref} vs {cam}")
        ax_al.legend(fontsize=8)

    plt.tight_layout()
    plt.show()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("root", type=Path,
                    help="mission folder or parent folder containing multiple missions")
    ap.add_argument("--max-lag", type=float, default=30.0, metavar="S",
                    help="maximum offset to search in seconds (default: 30)")
    ap.add_argument("--dt", type=float, default=0.005, metavar="S",
                    help="resampling interval in seconds (default: 0.005 = 200 Hz)")
    ap.add_argument("--plot", action="store_true",
                    help="show diagnostic plots per mission (requires matplotlib)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute offsets but do not write to metadata.json")
    ap.add_argument("--force", action="store_true",
                    help="re-run even if gyro_offsets_s already in metadata.json")
    args = ap.parse_args()

    missions = find_missions(args.root, exit_on_empty=True)
    print(f"Found {len(missions)} mission(s) under {args.root.resolve()}")

    for mission in missions:
        process_mission(
            mission,
            dt=args.dt,
            max_lag=args.max_lag,
            dry_run=args.dry_run,
            force=args.force,
            do_plot=args.plot,
        )

    print(f"\nDone — processed {len(missions)} mission(s).")


if __name__ == "__main__":
    main()
