# Requirements

**Python 3.8+** and **ffmpeg** (system package):

```bash
sudo apt install ffmpeg
```

**Python packages:**

```bash
pip install -r requirements.txt
```

| Package | Used by |
|---------|---------|
| `numpy` | `sync_gyro.py`, `overlay_stats.py`, `extract_telemetry.py` (plots) |
| `matplotlib` | `extract_telemetry.py` (plots), `sync_gyro.py` (`--plot`) |
| `pyyaml` | `overlay_stats.py`, `crop_missions.py` |
| `rosbags` | `extract_ros_imu.py`, `overlay_stats.py` |

`matplotlib` and `numpy` are optional for `extract_telemetry.py` — plots are skipped if not installed. `rosbags` is only needed for ROS bag workflows.

Not required but a VS Code CSV editor is really nice ([ReprEng.csv](https://marketplace.visualstudio.com/items?itemName=ReprEng.csv))

# GoPro Mission Organizer

Organizes multi-camera GoPro footage into named mission folders. Matches recordings across cameras by start time and duration, produces an editable CSV for human review, then moves files only when you approve.


## Folder structure expected

```
footage/
  Front/    GX010147.MP4  GL010147.LRV  GX010147.THM  ...
  Left/     GX010304.MP4  ...
  Right/    GX010167.MP4  ...
```

Each subfolder is treated as one camera mount. The folder name becomes the suffix on renamed files (e.g. `GX010147_Front.MP4`). Camera names are discovered automatically — no configuration needed.

## Typical workflow

### 1. Dry-run to see what the script proposes

```bash
python3 organize_missions.py /path/to/footage
```

Shows a mission matrix — one row per detected mission, one column per camera — and the full list of planned moves. Nothing is changed.

### 2. Export the plan to a CSV for review

```bash
python3 organize_missions.py /path/to/footage --export /path/to/footage/mission_plan.csv
```

Open `mission_plan.csv` in any spreadsheet app. The columns are:

| Column | Meaning |
|--------|---------|
| `mission (edit names)` | Folder name to create — **edit this** to name your missions |
| `start` | Anchor recording start time |
| `# Cameras` | How many cameras are in this mission |
| `dur` | Anchor recording duration |
| `dur_spread` | Max duration gap between cameras (small = confident match) |
| `start_spread` | Max start-time gap between cameras (seconds) |
| `Short Video` | `X` if any recording is under 30 s — likely a test clip |
| `Single Camera` | `X` if only one camera recorded this |
| `Near Start Tol` | `X` if start spread is close to the tolerance limit |
| `Near Dur Tol` | `X` if duration spread is close to the tolerance limit |
| `{Camera}` | **Editable** — video number assigned from that camera |
| `{Camera}_Δt` | How many seconds after anchor this camera started |

**Editing rules:**
- Rename a mission: change the `mission (edit names)` cell
- Remove a mission: delete the entire row — those files stay in their camera folder and will be collected into `other/`
- Remove a camera from a mission: clear its video# cell
- The `start`, `dur`, spread, warning, and `_Δt` columns are informational — the script ignores them on import

### 3. Import your edited plan and verify

```bash
python3 organize_missions.py /path/to/footage --import /path/to/footage/mission_plan.csv
```

Shows the updated matrix and all planned moves. Still dry-run — nothing changes.

### 4. Execute

```bash
python3 organize_missions.py /path/to/footage --import /path/to/footage/mission_plan.csv --execute
```

Files are moved (never copied). The result looks like:

```
footage/
  DiveArea/
    GX010147_Front.MP4   GL010147_Front.LRV   GX010147_Front.THM
    GX010304_Left.MP4    GL010304_Left.LRV    GX010304_Left.THM
    GX010167_Right.MP4   GL010167_Right.LRV   GX010167_Right.THM
    ...
  Plane/
    ...
  other/
    GX010305_Left.MP4    ← recordings not assigned to any mission
    ...
```

A hidden `.gopro_mission` marker file is written into each output folder so the script won't treat them as camera inputs on future runs.

## All flags

```
python3 organize_missions.py <root> [options]

  --export FILE       Export auto-clustered plan to CSV for review
  --import FILE       Load missions from an edited CSV instead of auto-clustering
  --execute           Actually move files (default is dry-run)
  --no-other          Leave unassigned recordings in place instead of moving to other/
  --timeline          Print per-recording metadata debug table (shows ffprobe/mtime source)
  --verbose           Print expanded per-recording detail inside each mission
  --start-tol N       Max start-time gap in seconds to group recordings (default 60)
  --dur-tol N         Max duration difference in seconds to group recordings (default 120)
```

## Tunable parameters (top of script)

```python
START_TOL_S   = 60.0   # max start-time gap to call two recordings the same mission
DUR_TOL_S     = 120.0  # max duration difference to call two recordings the same mission
WARN_TOL_FRAC = 0.6    # warn when spread exceeds this fraction of the tolerance
SHORT_WARN_S  = 30.0   # warn if any recording is shorter than this (seconds)
MISSION_PREFIX = ""    # prepended to every mission folder name; "" = no prefix
```

## GoPro filename convention

```
GX + CC + NNNN + .MP4    (video chapter — CC is 2-digit chapter, NNNN is 4-digit video#)
GL + CC + NNNN + .LRV    (low-res proxy)
GX + CC + NNNN + .THM    (thumbnail)
```

Files sharing the same `NNNN` across multiple chapter numbers (`GX01`, `GX02`, `GX03`, …) are treated as one continuous recording. All chapters, proxies, and thumbnails for a recording move together.

---

# GoPro Mission Compactor

After organizing footage with `organize_missions.py`, run `compact_missions.py` to concatenate chapter files from each camera into single videos, and archive the originals.

GoPros split long recordings into ~4 GB chapters (`GX01`, `GX02`, `GX03`, …). This script joins them back into one file per camera per mission.

## Typical workflow

### 1. Dry-run to see what will happen

```bash
python3 compact_missions.py /path/to/footage
```

Shows what would be concatenated and moved for every mission folder. Nothing is changed.

### 2. Execute

```bash
python3 compact_missions.py /path/to/footage --execute
```

For each mission folder:

- **Multi-chapter cameras** (e.g. `GX010147_Front.MP4` + `GX020147_Front.MP4`): concatenated into `Front.MP4` using lossless stream copy. Originals move to `raw/`.
- **Single-chapter cameras** (e.g. `GX010149_Front.MP4`): left exactly as-is with the original GX filename.
- **LRV proxy files**: always moved to `raw/` (they are already compressed — see `--lrv` below).
- **THM thumbnail files**: always moved to `raw/`.

Result:

```
footage/
  DiveArea/
    Front.MP4                 ← concatenated full-res (3 chapters joined)
    GX010304_Left.MP4         ← single chapter, left as-is
    GX010167_Right.MP4        ← single chapter, left as-is
    raw/
      GX010147_Front.MP4      ← original chapter 1
      GX020147_Front.MP4      ← original chapter 2
      GX030147_Front.MP4      ← original chapter 3
      GL010147_Front.LRV      ← all LRV files
      GX010147_Front.THM      ← all THM files
      ...
      .gopro_mission
    .gopro_mission
```

### 3. Execute with LRV proxy compilation (optional)

```bash
python3 compact_missions.py /path/to/footage --execute --lrv
```

LRV files are low-resolution proxy videos (already compressed by the GoPro). With `--lrv`:

- **Multi-chapter LRVs**: concatenated into `Front_LRV.MP4` (MP4 extension so any player opens it).
- **Single-chapter LRVs**: renamed in-place from `GL010147_Front.LRV` → `GL010147_Front_LRV.MP4`. Not moved to `raw/`.
- Multi-chapter LRV originals still move to `raw/`.

## All flags

```
python3 compact_missions.py <root> [options]

  --execute       Actually perform operations (default is dry-run)
  --lrv           Also process LRV proxy files (concat multi-chapter, rename single-chapter)
  --force         Overwrite existing output files
```

---

# Synchronized Multi-Camera Crop

`crop_missions.py` crops every camera in a mission to a single time window that you specify on the **reference camera's** timeline. Each other camera is cropped to the same real-world moments using its measured clock offset, so the resulting clips stay in sync.

You give times relative to the reference video (the one with offset 0.0 in `metadata.json`). The script applies each camera's offset automatically — preferring the gyro-derived offset (`gyro_offsets_s`, sub-frame accurate) and falling back to the creation-time offset (`sync_offsets_s`) when no gyro offset exists.

Requires `data/metadata.json` with sync offsets — run `extract_telemetry.py` and `sync_gyro.py` first.

### 1. Dry-run (default) to preview the plan

```bash
python3 crop_missions.py /path/to/Mission --start 200 --end 400
```

Times accept seconds (`200`), `MM:SS` (`3:20`), or `HH:MM:SS` (`1:03:20`). Example output:

```
[Plane]
  Reference camera: Front
  Window on reference: 3:20.000 – 6:40.000  (200.00s)
  Front MP4: Front.MP4  offset=REF
      crop 3:20.000 +200.00s -> Front.MP4
  Left MP4: GX010306_Left.MP4  offset=+6.581s [gyro]
      crop 3:13.419 +200.00s -> Left.MP4
  Right MP4: GX010168_Right.MP4  offset=+4.675s [gyro]
      crop 3:15.325 +200.00s -> Right.MP4
```

### 2. Include LRV proxies

```bash
python3 crop_missions.py /path/to/Mission --start 200 --end 400 --lrv
```

Also crops each camera's LRV proxy to `{camera}_LRV.MP4`.

### 3. Execute

```bash
python3 crop_missions.py /path/to/Mission --start 200 --end 400 --lrv --execute
```

For each camera:
- The cropped video is written as `{camera}.MP4` (and `{camera}_LRV.MP4`)
- The pre-crop original is moved into `raw/`

If the mission has an `overlays.yaml`, the script also prints the matching `overlay_stats.py --crop-offset` command to regenerate the stats overlay aligned to the cropped clip (see [Subclip alignment](#subclip-alignment)).

### The crop record (`crop.yaml`) and re-cropping

On execute, the script writes a `crop.yaml` into the mission folder recording exactly what was done — the reference camera, the window (on the reference timeline), the method, and per-camera details including where each original now lives in `raw/`:

```yaml
reference_camera: Front
window:
  start_s: 665.0
  end_s: 1680.0
  duration_s: 1015.0
  start_hms: '11:05.000'
  end_hms: '28:00.000'
method: stream-copy
lrv: true
crops:
  - camera: Front
    output: Front.MP4
    original: raw/Front.MP4
    offset_s: 0.0
    offset_src: ref
    crop_start_s: 665.0
    crop_dur_s: 1015.0
  - camera: Left
    output: Left.MP4
    original: raw/GX010306_Left.MP4
    offset_s: 6.581
    offset_src: gyro
    crop_start_s: 658.419
    crop_dur_s: 1015.0
  # ...
```

**To change the crop, just run it again with a new window.** When a `crop.yaml` exists, the script re-cuts from the pristine **originals in `raw/`** (not the already-cropped files), so you can adjust the window as many times as you like without quality loss or drift:

```bash
python3 crop_missions.py /path/to/Mission --start 700 --end 1500 --lrv --execute
```

The originals in `raw/` are never touched, and `crop.yaml` is rewritten with the new window. `metadata.json` is never modified — it keeps describing the originals.

### Crop accuracy: stream copy vs. re-encode

By default the crop uses **stream copy** — fast and lossless, but each cut lands on the nearest keyframe at or before the requested start (sub-second imprecision, and it can differ slightly per camera).

Cropped clips contain video + audio only; the GoPro timecode and GPMF telemetry data streams are dropped (telemetry is already extracted to the `data/` CSVs).

For frame-accurate cuts, add `--reencode` (re-encodes video with libx265, copies audio):

```bash
python3 crop_missions.py /path/to/Mission --start 200 --end 400 --reencode --execute
```

This is much slower on full-res 5K footage. A common workflow is to crop the LRV proxies frame-accurately for review (`--lrv --reencode`) and stream-copy the full-res files.

### All flags

```
python3 crop_missions.py MISSION --start T --end T [options]

  --start T       window start on reference timeline (sec / MM:SS / HH:MM:SS)
  --end T         window end on reference timeline
  --lrv           also crop LRV proxy videos
  --reencode      frame-accurate crop via re-encode (default: fast stream copy)
  --execute       actually perform the crop (default: dry-run)
  --force         overwrite if a pre-crop original already exists in raw/
```

`MISSION` may be a single mission folder or a parent folder containing several — missions without sync offsets are skipped.

### Edge cases

If the window starts before a camera began recording (or ends after it stopped), that camera's clip is clamped to its available footage and a warning is printed. The clamped clip aligns at the end (for a late start) or at the start (for an early end).

---

# GoPro Telemetry Extractor

After compacting missions, run `extract_telemetry.py` to pull gyroscope, accelerometer, gravity, and orientation data out of the GPMF track embedded in each GoPro MP4 and write them to `{mission}/data/` as CSVs and plots.

```bash
python3 extract_telemetry.py /path/to/footage
```

Re-extract a mission that already has a `data/` folder:

```bash
python3 extract_telemetry.py /path/to/footage --force
```

Output written per mission:

```
{mission}/data/
  metadata.json          ← sizes, durations, creation times, sync offsets
  {camera}_gyro.csv      ← t_s, gx_rads, gy_rads, gz_rads
  {camera}_accl.csv      ← t_s, ax_ms2, ay_ms2, az_ms2
  {camera}_grav.csv      ← t_s, gx, gy, gz  (normalised gravity)
  {camera}_cori.csv      ← t_s, w, x, y, z  (camera orientation quaternion)
  {camera}_iori.csv      ← t_s, w, x, y, z  (image orientation quaternion)
  plots/
    all_cameras_accel_magnitude.png
    {camera}_accl.png
    {camera}_gyro.png
    ...
```

---

# Inter-Camera Sync (gyroscope cross-correlation)

`sync_gyro.py` estimates how many seconds each camera's clock was ahead of or behind the reference camera, using the gyroscope magnitude `|ω| = √(gx²+gy²+gz²)`. Because all cameras are rigidly mounted, they share the same rotation — cross-correlating `|ω|` finds the clock offset without needing to know the physical rotation between cameras.

Results are written into `{mission}/data/metadata.json` as `gyro_offsets_s`.

```bash
python3 sync_gyro.py /path/to/footage
```

Options:

```
  --max-lag S    Maximum offset to search in seconds (default: 30)
  --dt S         Resampling interval in seconds (default: 0.005 = 200 Hz)
  --plot         Show diagnostic cross-correlation plots (requires matplotlib)
  --dry-run      Print results without writing to metadata.json
  --force        Re-run even if gyro_offsets_s already present
```

---

# ROS Bag IMU Extraction

`extract_ros_imu.py` reads a `sensor_msgs/msg/Imu` topic from a ROS bag and writes gyro and accel CSVs in the same format as `extract_telemetry.py`. Placing the output in a mission's `data/` folder lets `sync_gyro.py` automatically cross-correlate the ROS IMU against the GoPro cameras to find the bag-to-camera time offset.

Supports ROS1 (`.bag`) and ROS2 (`.db3` / `.mcap`) bag files. Requires the `rosbags` Python package.

### Install

```bash
pip install rosbags
```

### Scan an entire footage directory (typical usage)

Pass the root folder containing all mission subfolders. The script finds every ROS bag and writes CSVs into the corresponding mission's `data/` folder automatically:

```bash
python3 extract_ros_imu.py /path/to/footage/
```

Each mission is expected to contain exactly one ROS bag. Already-extracted missions are skipped:

```
Found 5 bag(s) under /path/to/footage
  ball_3.0-...: reading /bluerov2/imu/data ...
    216299 samples  1088.5 s  ~199 Hz
    → .../Ball/data/bluerov2_gyro.csv
    → .../Ball/data/bluerov2_accl.csv
  plane_2.0-...: already extracted — skipping (use --force to overwrite)
...
Done — 4/5 bag(s) extracted.
```

### Single bag

```bash
python3 extract_ros_imu.py /path/to/MissionName/bag_directory/
```

Writes CSVs to `MissionName/data/` by default, or to a custom location with `--out`:

```bash
python3 extract_ros_imu.py /path/to/bag_directory/ --out /custom/output/dir/
```

### Custom topic

```bash
python3 extract_ros_imu.py /path/to/footage/ --topic /my_robot/imu/data
```

If the topic is not found in a bag, the script prints the available topics and continues to the next bag.

### ROS2 multi-file bags

ROS2 bags are often split into many `.mcap` segments inside a single directory (with a `metadata.yaml` alongside them). Pass the **directory**, not an individual file — this is the default layout and is handled automatically.

---

# Video Stats Overlay (ASS subtitles)

`overlay_stats.py` generates an ASS subtitle file that overlays live ROS bag data on your GoPro video — no re-encoding needed. Works with any ROS topic and any message field. Load the `.ass` file alongside the video in VLC, mpv, or DaVinci Resolve.

### Install

```bash
pip install pyyaml
```

### 1. Discover what topics are in your bag

```bash
python3 overlay_stats.py /path/to/MissionName --list-topics
```

### 2. Create a config file

Create `overlays.yaml` in the mission folder:

```yaml
bag: bag_directory_name         # ROS2 bag dir (relative to mission) or absolute path
bag_offset_s: 150.045           # seconds the bag started AFTER the GoPro reference camera
camera: Front                   # which camera's video to target

font_size: 40     # 40+ for 5K video; 16-20 for LRV proxy
line_height: 50   # vertical spacing between stacked overlays

overlays:
  - topic: /bluerov2/imu/data
    field: angular_velocity.x   # dot-notation into the message (supports array[0] indexing)
    label: "Gyro X"
    unit: "rad/s"
    format: ".3f"
    position: top-left          # top-left / top-right / bottom-left / bottom-right / top-center / bottom-center
    color: "#00FF00"

  - topic: /bluerov2/dvl/data
    field: velocity.x
    label: "DVL Vx"
    unit: "m/s"
    format: ".3f"
    position: top-right
    color: "#FF8800"
```

Multiple overlays at the same `position` stack vertically. `enabled: false` hides an overlay without deleting it.

### 3. Generate the subtitle file

```bash
python3 overlay_stats.py /path/to/MissionName
```

Output: `MissionName/overlays/Front_stats.ass`

### 4. View with mpv

```bash
mpv Front_LRV.MP4 --sub-file=overlays/Front_stats.ass
```

Or in VLC: **Subtitle → Add Subtitle File**. In DaVinci Resolve: import as a subtitle track.

### Subclip alignment

There are two ways to align the overlay with a trimmed clip:

**Before cropping** — generate against the full video with `--start` / `--end`. Output timestamps are shifted to start at t=0, matching a clip you'll trim to that range:

```bash
python3 overlay_stats.py /path/to/MissionName --start 200 --end 600
```

**After cropping with `crop_missions.py`** — use `--crop-offset` set to the crop's `--start`. This adjusts the bag alignment for a video that already begins partway into the original timeline (it subtracts the offset from `bag_offset_s`):

```bash
# you cropped with: crop_missions.py MissionName --start 200 --end 600 --execute
python3 overlay_stats.py /path/to/MissionName --crop-offset 200 --force
```

`crop_missions.py` prints this exact command after a crop when an `overlays.yaml` is present.

### Other options

```
  --config PATH     alternate config file location
  --camera NAME     override camera from config
  --force           overwrite existing output
  --dry-run         print plan without writing files
```

**Font size guidance:** The overlay uses `PlayResX/Y` set to the video's actual resolution. At 5K (5312×2988), use `font_size: 40`+. For LRV proxies (~540p), use `font_size: 16`–`20`.

**N/A display:** Values show "N/A" when the video timestamp falls before the bag started or after it ended. The `bag_offset_s` in the config controls this boundary — get the value from `sync_gyro.py` output.

---

### Syncing the ROS IMU against GoPro cameras

Once the CSV is in `data/`, run `sync_gyro.py` as normal. If the bag started more than 30 s into the GoPro recording, increase `--max-lag` accordingly:

```bash
python3 sync_gyro.py /path/to/MissionName --max-lag 200 --dry-run
```

The output includes the ROS IMU offset against the reference camera and pairwise consistency checks against every other camera.

**Units:** Both the GoPro GPMF stream and `sensor_msgs/Imu` report gyro in **rad/s** and accel in **m/s²** — no conversion is needed.

**Timestamp note:** The script uses `msg.header.stamp` (the sensor's own clock), not the bag record timestamp. The bag timestamp lags the header stamp by 4–25 ms due to OS scheduling jitter and is not suitable for precision synchronisation.

---

## GPS and telemetry data

GoPro cameras embed GPS, gyroscope, accelerometer, and temperature data as a **GPMF** (GoPro Metadata Format) binary track inside the MP4 container alongside the video and audio. This script uses **lossless stream copy** (`ffmpeg -c copy`), which preserves the GPMF track completely — GPS data is not lost during concatenation.

To extract the GPS track from a concatenated file to a standard GPX file:

```bash
pip install gopro2gpx
gopro2gpx -s Front.MP4 Front.gpx
```

Note: `exiftool -GPS* Front.MP4` shows only the starting-point coordinates. For the full GPS track, use `gopro2gpx`.

## Safety

- **Dry-run by default.** `--execute` must be passed explicitly.
- **Originals protected.** Files are only moved to `raw/` after the output is verified to exist and have a non-zero size. If ffmpeg fails, nothing is moved.
- **Idempotent.** If `raw/` already exists in a mission folder, the script skips it. Re-running is safe.
- **Already-organized folders only.** Only folders containing a `.gopro_mission` marker (written by `organize_missions.py --execute`) are processed.

---

## Safety

- **Dry-run by default.** `--execute` must be passed explicitly.
- **Pre-flight check.** Before moving anything, the script verifies no two files would land on the same destination and no source has vanished.
- **Moves, never copies.** Files are not duplicated.
- **Re-runnable.** Output folders are excluded from camera discovery via the `.gopro_mission` marker, so re-running on a partially organized folder is safe.
