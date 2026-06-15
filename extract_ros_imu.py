#!/usr/bin/env python3
"""
extract_ros_imu.py — extract IMU data from ROS bag files.

Reads a sensor_msgs/msg/Imu topic and writes per-source CSVs:
  {out_dir}/{name}_gyro.csv   — t_s, gx_rads, gy_rads, gz_rads
  {out_dir}/{name}_accl.csv   — t_s, ax_ms2, ay_ms2, az_ms2

Where {name} is derived from the first component of the topic path
(e.g. /bluerov2/imu/data → bluerov2).

Timestamps are seconds from the first sample, taken from msg.header.stamp
(the sensor's own clock, not the bag record timestamp).

Supports both ROS1 (.bag) and ROS2 (.db3 / .mcap) bag files.

Usage:
  # Single bag file or ROS2 bag directory:
  python extract_ros_imu.py /path/to/bag_dir/ [--topic TOPIC] [--out DIR]

  # Scan a root directory for all bags and extract into each mission's data/ folder:
  python extract_ros_imu.py /path/to/SandHollow/ [--topic TOPIC] [--force]
"""

import argparse
import csv
import sys
from pathlib import Path

from rosbags.highlevel import AnyReader


TOPIC_DEFAULT = "/bluerov2/imu/data"


def topic_to_name(topic: str) -> str:
    """Derive output filename prefix from topic, e.g. /bluerov2/imu/data → bluerov2."""
    parts = [p for p in topic.split("/") if p]
    return parts[0] if parts else "ros_imu"


def is_ros2_bag(path: Path) -> bool:
    return path.is_dir() and (path / "metadata.yaml").exists()


def is_ros1_bag(path: Path) -> bool:
    return path.is_file() and path.suffix == ".bag"


def find_bags(root: Path) -> list[Path]:
    """Return all ROS bag paths under root (ROS2 dirs and ROS1 .bag files)."""
    bags: list[Path] = []
    # ROS2 bags: any directory containing metadata.yaml
    ros2 = sorted(p.parent for p in root.rglob("metadata.yaml"))
    bags.extend(ros2)
    # ROS1 bags: .bag files not inside a ROS2 bag directory
    ros2_set = set(ros2)
    for p in sorted(root.rglob("*.bag")):
        if not any(p.is_relative_to(d) for d in ros2_set):
            bags.append(p)
    return bags


def extract_imu(bag_path: Path, topic: str, out_dir: Path, force: bool) -> bool:
    """
    Extract gyro and accel CSVs from bag_path. Returns True on success.
    Skips silently if outputs already exist and force=False.
    """
    name = topic_to_name(topic)
    gyro_path = out_dir / f"{name}_gyro.csv"
    accl_path = out_dir / f"{name}_accl.csv"

    if not force and gyro_path.exists() and accl_path.exists():
        print(f"  {bag_path.name}: already extracted — skipping (use --force to overwrite)")
        return False

    gyro_rows: list[tuple[float, float, float, float]] = []
    accl_rows: list[tuple[float, float, float, float]] = []

    try:
        with AnyReader([bag_path]) as reader:
            connections = [c for c in reader.connections if c.topic == topic]
            if not connections:
                available = sorted({c.topic for c in reader.connections})
                print(f"  {bag_path.name}: topic '{topic}' not found — skipping")
                print(f"    available topics: {', '.join(available[:8])}"
                      + (" ..." if len(available) > 8 else ""))
                return False

            print(f"  {bag_path.name}: reading {topic} ...")
            for connection, _bag_ts, rawdata in reader.messages(connections=connections):
                msg = reader.deserialize(rawdata, connection.msgtype)
                stamp = msg.header.stamp
                t_s = stamp.sec + stamp.nanosec * 1e-9
                av = msg.angular_velocity
                la = msg.linear_acceleration
                gyro_rows.append((t_s, av.x, av.y, av.z))
                accl_rows.append((t_s, la.x, la.y, la.z))
    except Exception as e:
        print(f"  {bag_path.name}: error reading bag — {e}")
        return False

    if not gyro_rows:
        print(f"  {bag_path.name}: no messages on topic — skipping")
        return False

    t0 = gyro_rows[0][0]
    gyro_rows = [(r[0] - t0, r[1], r[2], r[3]) for r in gyro_rows]
    accl_rows = [(r[0] - t0, r[1], r[2], r[3]) for r in accl_rows]

    out_dir.mkdir(parents=True, exist_ok=True)

    with open(gyro_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "gx_rads", "gy_rads", "gz_rads"])
        w.writerows(gyro_rows)

    with open(accl_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "ax_ms2", "ay_ms2", "az_ms2"])
        w.writerows(accl_rows)

    dur = gyro_rows[-1][0]
    hz = len(gyro_rows) / dur if dur > 0 else 0
    print(f"    {len(gyro_rows)} samples  {dur:.1f} s  ~{hz:.0f} Hz")
    print(f"    → {gyro_path}")
    print(f"    → {accl_path}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("path", type=Path,
                    help="ROS bag (file or ROS2 directory), or root directory to scan for bags")
    ap.add_argument("--topic", default=TOPIC_DEFAULT,
                    help=f"IMU topic to extract (default: {TOPIC_DEFAULT})")
    ap.add_argument("--out", type=Path, default=None, metavar="DIR",
                    help="output directory; only valid when pointing at a single bag "
                         "(default: {bag_parent}/data/)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing CSV files")
    args = ap.parse_args()

    path: Path = args.path
    if not path.exists():
        sys.exit(f"error: path not found: {path}")

    # Determine mode: single bag vs. directory scan
    if is_ros2_bag(path) or is_ros1_bag(path):
        bags = [path]
        out_dirs = {path: args.out or path.parent / "data"}
    elif path.is_dir():
        if args.out is not None:
            sys.exit("error: --out cannot be used when scanning a root directory")
        bags = find_bags(path)
        if not bags:
            sys.exit(f"error: no ROS bags found under {path}")
        # Output goes into the bag's parent's data/ folder
        out_dirs = {b: b.parent / "data" for b in bags}
        print(f"Found {len(bags)} bag(s) under {path.resolve()}")
    else:
        sys.exit(f"error: not a ROS bag or directory: {path}")

    ok = total = 0
    for bag in bags:
        total += 1
        if extract_imu(bag, args.topic, out_dirs[bag], args.force):
            ok += 1

    if total > 1:
        print(f"\nDone — {ok}/{total} bag(s) extracted.")


if __name__ == "__main__":
    main()
