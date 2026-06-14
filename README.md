# Requirements

- Python 3.8+
- `ffmpeg` 

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
