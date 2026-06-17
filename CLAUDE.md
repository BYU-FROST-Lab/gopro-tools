# CLAUDE.md — GoPro Mission Tools

Notes for AI-assisted development of the GoPro mission toolchain.

## File layout

```
utils.py                ← shared constants, dataclasses, ffprobe helpers
organize_missions.py    ← step 1: sort raw camera files into mission folders
compact_missions.py     ← step 2: concatenate chapters, archive originals
extract_telemetry.py    ← step 3: extract GPMF telemetry, generate plots & metadata
sync_gyro.py            ← step 4: estimate inter-camera clock offsets via gyro cross-correlation
extract_ros_imu.py      ← step 5: extract IMU data from a ROS2 bag into mission data/ folders
overlay_stats.py        ← step 6: generate ASS subtitle overlay from any ROS bag topic/field
crop_missions.py        ← step 7: crop all cameras to a synced time window, archive originals
run_pipeline.py         ← orchestrator: chains all steps over a root, pausing at 2 human checkpoints
```

## What each script does

**`organize_missions.py`** — Scans a folder of GoPro camera subfolders, groups recordings across cameras into "missions" by matching start times and durations, produces a reviewable CSV, and moves files into named output folders after human approval.

**`compact_missions.py`** — Scans a folder of already-organized mission subfolders (identified by `.gopro_mission` marker), concatenates chapter files per camera into single videos using lossless stream copy, and moves all originals to a `raw/` subfolder.

**`utils.py`** — Shared code imported across the toolchain. Constants/types: `MISSION_MARKER`, `NAME_RE`, `COMPACT_RE`, `SIMPLE_RE`, `VIDEO_EXTS`, `PROXY_EXTS`, `THUMB_EXTS`, `FileEntry`, `Recording`. Metadata helpers: `ffprobe()`, `thm_creation()`, `mtime()`, `best_start()`, `total_duration()`. Mission/IO helpers (added in the robustness pass): `find_missions(root, exit_on_empty=False)`, `is_mission()`, `mission_compacted()`, `load_metadata(mission, default=None)`, `save_metadata()` (atomic), `atomic_write_text()`, `atomic_write_bytes()`, `parse_time()`, `fmt_time()`. Any future tool should import from here rather than re-implementing — the discovery, metadata, and regex logic used to be copy-pasted (and subtly diverged) across five scripts.

## Robustness conventions (crash / re-run safety)

Two rules keep the pipeline safe to interrupt and re-run, and every script follows them:

1. **Atomic writes.** No script writes a generated file directly to its final path. All text/JSON/CSV/`.ass`/`crop.yaml`/GPX outputs go through `utils.atomic_write_text` (write a sibling `.tmp`, then `os.replace`). ffmpeg outputs go to a sibling temp (`{stem}.concat.tmp{ext}` in compact, `{stem}.cropping{ext}` in crop) and are `os.replace`'d in only after a non-zero, non-empty result. So a killed process never leaves a truncated final file that would corrupt downstream steps.

2. **Completion is signalled by the LAST artifact, written atomically.** Resume/skip checks (and `run_pipeline.py`'s `mission_state`) key off that artifact, never off an early-created one:
   - compact → `raw/.gopro_mission` marker, written *after* all chapter moves (`utils.mission_compacted`). A `raw/` that exists *without* the marker means an interrupted run; compact refuses to silently skip it and asks for `--force`.
   - telemetry → a valid `data/metadata.json` (`utils.load_metadata(...) is not None`), written last. A partial extraction (stray CSVs, no metadata) re-extracts cleanly.
   - sync → `gyro_offsets_s` in metadata; crop → `crop.yaml`; ros → both gyro+accl CSVs (written back-to-back after all data is in memory, so you never get only one).

   `utils.load_metadata` never raises — a corrupt/half-written `metadata.json` reads as the default, so callers treat the step as not-done rather than crashing.

Known limitation: fully automatic mid-*compact* resume isn't attempted (originals may already be in `raw/`); the guarantees are "no corrupt output", "originals preserved in raw/", and "never silently mark an incomplete mission done".

**`extract_telemetry.py`** — Reads GPMF binary streams from raw chapter MP4 files (from `raw/` subfolders or main folder if not compacted), extracts telemetry data, and writes results into `{mission}/data/`. Does not modify any video files.

**`sync_gyro.py`** — Estimates temporal offsets between cameras (and any other IMU source in `data/`) by cross-correlating gyroscope magnitude `|ω|`. Writes `gyro_offsets_s` into `metadata.json`. Any `*_gyro.csv` placed in `{mission}/data/` is automatically included — including `bluerov2_gyro.csv` produced by `extract_ros_imu.py`.

**`crop_missions.py`** — Crops every camera in a mission to a time window given on the *reference* camera's timeline, keeping all cameras synchronized via their measured clock offsets. Cropped output is named `{camera}.MP4` (and `{camera}_LRV.MP4` with `--lrv`); the pre-crop original is moved into `raw/`. Dry-run by default. Imports `ffprobe` from `utils.py`.

**`overlay_stats.py`** — Reads any topic/field from a ROS bag and generates an ASS subtitle file that overlays the values on a GoPro video. No re-encoding — load the `.ass` file alongside the video in VLC, mpv, or DaVinci Resolve. Requires `pyyaml` (`pip install pyyaml`).

**`extract_ros_imu.py`** — Reads a ROS bag file (ROS1 `.bag` or ROS2 `.db3`/`.mcap`) and extracts `angular_velocity` and `linear_acceleration` from a `sensor_msgs/msg/Imu` topic. Writes `{name}_gyro.csv` and `{name}_accl.csv` in the same format as `extract_telemetry.py`, so `sync_gyro.py` can consume them directly. Requires the `rosbags` library (`pip install rosbags`).

**`run_pipeline.py`** — Orchestrator that drives the whole toolchain over a root folder so you don't run the seven scripts by hand. Each step is invoked as a **subprocess** (using `sys.executable`), so heavy/optional deps (matplotlib, rosbags, numpy) stay isolated. It normalizes the scripts' inconsistent `--execute`-vs-`--dry-run` conventions behind a single `--execute` flag (orchestrator is **dry-run by default**), and pauses at the two real human checkpoints: (1) review/edit `mission_plan.csv` before organize moves files; (2) fill start/end per mission in `crop_plan.csv` before cropping. See the architecture section below.

## organize_missions.py architecture

```
discover()
  → list[Recording], list[orphan]

cluster_missions(recordings)          ← auto mode
import_missions_csv(file, recordings) ← --import mode
  → list[list[Recording]], list[names]

build_plan(root, missions, orphans, names, leftovers)
  → list[(src_path, dst_path)]

print_matrix / print_timeline / print_plan   ← reporting only
export_plan_csv                              ← writes annotated CSV
execute(plan)                                ← only with --execute
```

## compact_missions.py architecture

```
find_missions(root)
  → list[mission_folder_path]       ← finds subfolders with .gopro_mission marker

parse_mission_files(folder)
  → dict[(camera, ext) -> [(chapter_int, path), ...]]

ffmpeg_concat(chapter_paths, output_path, dry_run, output_fmt=None)
  → bool                            ← stream copy via concat demuxer; -copy_unknown preserves GPMF

compact_mission(folder, lrv, dry_run, force)
  → bool
```

**compact_missions.py file-handling rules:**

| File type | Without `--lrv` | With `--lrv` |
|-----------|-----------------|--------------|
| MP4, multi-chapter | concat → `{camera}.MP4`, originals → `raw/` | same |
| MP4, single-chapter | leave GX filename as-is | same |
| LRV, multi-chapter | → `raw/` | concat → `{camera}_LRV.MP4`, originals → `raw/` |
| LRV, single-chapter | → `raw/` | rename in-place `GL…_LRV.MP4`, stay in folder |
| THM, any | → `raw/` | → `raw/` |

**COMPACT_RE** — separate regex for organized files (post-`organize_missions.py`):
`^(?P<prefix>G[XLH])(?P<chapter>\d{2})(?P<video>\d{4})_(?P<camera>[^.]+)\.(?P<ext>[A-Za-z0-9]+)$`
This is distinct from `NAME_RE` in `utils.py` which matches pre-organization filenames.

## Key data structures

**`FileEntry`** — one file on disk (MP4, LRV, or THM). Fields: `path`, `camera`, `prefix`, `chapter`, `video` (4-char string), `ext`.

**`Recording`** — all chapters of one video# from one camera. Fields: `camera`, `video`, `files: list[FileEntry]`, `start` (epoch float), `start_src` (how start was obtained), `duration` (float seconds), `dur_src`.

**`missions`** — `list[list[Recording]]`. Each inner list is one mission; order within the inner list doesn't matter. A Recording appears in at most one mission.

**`mission_names`** — parallel `list[str]` of folder name suffixes. `None` when auto-clustering (names are computed from index). Index `i` of `names` corresponds to index `i` of `missions`.

## Important invariants

- **Video# is a 4-char string** (`"0147"`, not `147`). This preserves leading zeros. Never convert to int inside the script.
- **One recording per camera per mission.** `cluster_missions` enforces this; `import_missions_csv` does not check — if the user duplicates a camera in the CSV, the second is skipped with a warning.
- **Anchor-based clustering, not chaining.** Each mission is anchored to its first (earliest) recording. Later recordings must be within tolerance of the *anchor*, not of each other. This prevents drift across a long chain of near-misses. Do not change this to pairwise/chaining without understanding the implications.
- **Moves only, no copies.** `build_plan` produces `(src, dst)` pairs; `execute` calls `shutil.move`. Never use `shutil.copy`.
- **Output folders are marked.** `execute` writes a zero-byte `.gopro_mission` file into every folder it creates. `discover` checks for this marker to skip those folders on re-runs. This replaces the old `MISSION_PREFIX` prefix-check approach, which broke when the prefix was empty.

## Metadata fallback chain

For each recording's start time: `ffprobe creation_time` → THM file EXIF → filesystem mtime.
For duration: sum of `ffprobe duration` across chapters → mtime span between chapters (approximate).

`start_src` and `dur_src` fields record which source was used. The `--timeline` flag surfaces these. The `!W` warning flag (weak metadata) exists in `mission_warnings()` but is not currently emitted in the CSV export (removed when `_src` columns were dropped). Re-add to `export_plan_csv` if needed.

## CSV import/export contract

**Export** writes diagnostic columns that are ignored on import. **Import** finds camera columns by exact header name match against discovered cameras — position-independent. This means extra columns can be added to the CSV without breaking import.

The video# cell for each camera is the only cell that drives file moves. All other columns (`start`, `dur`, warning flags, `_Δt`) are informational.

Preamble rows (lines starting with `#`) and blank rows are skipped on import.

Video numbers are matched as-is first, then zero-padded to 4 digits as fallback (handles spreadsheet apps that strip leading zeros).

## Adding a new warning flag

1. Add detection logic in `mission_warnings(group) -> list[str]`
2. Add a column header in the `header` list in `export_plan_csv`
3. Add the corresponding `"X" if "!FLAG" in flags else ""` cell in the row-building loop
4. `import_missions_csv` needs no changes (it ignores non-camera columns)

## Adding support for a new file type

1. Add the extension to `VIDEO_EXTS`, `PROXY_EXTS`, or `THUMB_EXTS` as appropriate
2. `suffixed_name` handles all extensions uniformly — no changes needed there
3. If it's a new "primary" type (like MP4 is now), update the `has_mp4` check in `discover` and the `best_start` / `total_duration` functions

## Common extension points

**Custom mission prefix:** Set `MISSION_PREFIX = "mission_"` to get `mission_DiveArea` folders. Currently defaults to `""`.

**Tighter/looser matching:** Adjust `START_TOL_S` and `DUR_TOL_S` at top of file or via `--start-tol` / `--dur-tol` CLI args.

**Same-video time-gap split:** If a camera reuses a 4-digit video# after a card reformat, two separate sessions collapse into one Recording. The `--timeline` output makes this visible (one recording with chapters hours apart). To fix: add a split in `discover` that starts a new Recording when consecutive chapters have an mtime gap larger than some threshold.

**Multiple recordings per camera per mission:** Not currently supported. The clustering algorithm enforces one-per-camera. If this is ever needed, the `build_plan` destination naming would need to de-conflict files.

## Things not to change without care

- `NAME_RE` pattern — must match GoPro filename convention exactly
- The `id(r)` trick in `main()` for computing leftovers — works because Recording objects are unique Python objects; don't replace with value-based equality
- `execute` pre-flight checks — the abort-on-collision logic prevents data loss; don't weaken it

## extract_telemetry.py architecture

```
find_missions(root)
  → list[mission_folder_path]       ← finds subfolders with .gopro_mission marker

find_gpmf_sources(mission)
  → dict[camera -> list[Path]]      ← raw/ GX MP4s preferred; falls back to main folder
                                       GL (LRV proxy) files and *_LRV named files excluded

extract_gpmf_binary(mp4)
  → bytes | None                    ← ffmpeg stream copy of gpmd stream to stdout

parse_gpmf_packets(raw_bytes)
  → list[packet_dict]               ← one dict per DEVC block; contains stream data with STMP/SCAL

extract_timeseries(packets, stream_key)
  → (timestamps_s, scaled_rows)     ← STMP-based timing; values divided by SCAL

process_mission(mission, ...)
  → writes data/ subfolder          ← CSVs, GPX (if GPS5 present), PNG plots, metadata.json
```

**Output layout per mission:**
```
{mission}/
  data/
    metadata.json              ← per-camera sizes, durations, creation times, sync offsets
    {camera}_accl.csv          ← t_s, ax_ms2, ay_ms2, az_ms2
    {camera}_gyro.csv          ← t_s, gx_rads, gy_rads, gz_rads
    {camera}_grav.csv          ← t_s, gx, gy, gz (normalized gravity vector)
    {camera}_cori.csv          ← t_s, w, x, y, z (camera orientation quaternion / 32767)
    {camera}_iori.csv          ← t_s, w, x, y, z (image orientation quaternion / 32767)
    {camera}_gps.csv           ← t_s, lat_deg, lon_deg, alt_m, speed_ms, accuracy_m [if GPS5]
    {camera}.gpx               ← GPS track [if GPS5 present]
    plots/
      all_cameras_accel_magnitude.png  ← overlay for event detection / sync validation
      {camera}_accl.png
      {camera}_gyro.png
      {camera}_grav.png
      gps_all_cameras.png      ← track + altitude [if GPS5 present]
      summary.png              ← text card: sizes, durations, streams, sync offsets
```

**GPMF file discovery rules:**
- Scans both `raw/` and main mission folder; `raw/` GX chapters take priority per camera.
- Skips all GL-prefix files (LRV proxies) in both locations.
- Skips `*_LRV.MP4`-named compacted proxies (matched by SIMPLE_RE but filtered by `_LRV` suffix).
- Compacted `{camera}.MP4` files in main folder are used only when no `raw/` chapters exist for that camera; these files often lack GPMF if `compact_missions.py` didn't use `-copy_unknown`.

**GPMF timing model:**
- Each DEVC block carries a STMP (microseconds since recording start) per stream — the timestamp of the last sample in that block.
- Multi-chapter recordings: chapter offsets added so timestamps are continuous across chapters.
- Within a block, N samples are spaced evenly from the previous block's STMP to the current STMP.

**GPMF streams on HERO11 Black (these cameras):**
- No GPS5 stream — GPS appears to be disabled. GPS columns will be absent from CSVs.
- ACCL at ~200 Hz (m/s², scaled by SCAL ≈ 417)
- GYRO at ~200 Hz (rad/s, scaled by SCAL ≈ 939)
- GRAV at ~24 Hz (normalized gravity direction)
- CORI / IORI at ~24 Hz (orientation quaternions, /32767 → [-1, 1])

## GoPro telemetry (GPMF) and ffmpeg

GoPro MP4 files contain a third stream alongside video and audio: **GPMF** (GoPro Metadata Format), a binary data track carrying GPS, gyroscope, accelerometer, and temperature at high sample rates. Key facts:

- `ffmpeg -c copy` (stream copy) is supposed to preserve GPMF, but **in practice the compacted `{camera}.MP4` files produced by `compact_missions.py` are missing the gpmd stream** (confirmed on DiveArea: compacted `Front.MP4` has only 3 streams; raw `GX010147_Front.MP4` has 4 including gpmd). The originals in `raw/` always have GPMF intact — `extract_telemetry.py` reads from `raw/` specifically because of this.
- The warning `Could not find codec parameters for stream 2 (Unknown: none)` is expected. ffmpeg can't identify the GPMF codec but copies the raw bytes anyway. Adding `-copy_unknown` makes this explicit and ensures the stream is always included.
- Re-encoding video (`-c:v libx265` etc.) will silently drop GPMF unless you add `-map 0 -c:d copy`.
- `.LRV` files are standard MP4 containers with a renamed extension. ffmpeg cannot auto-detect the output format from the `.LRV` extension — pass `-f mp4` explicitly, or output to a `.MP4` filename (current approach: `{camera}_LRV.MP4`).
- **Do not install `gopro2gpx` or `gpmf-parser` as dependencies.** `extract_telemetry.py` contains a native GPMF binary parser that handles all streams (GPS5, ACCL, GYRO, GRAV, CORI, IORI) without external packages. `exiftool -GPS*` only returns the starting-point coordinates, not the full track.

## crop_missions.py architecture

```
find_missions(root)
  → [mission, ...]                ← root itself if marked, else marked subfolders

load_metadata(mission)            → dict | None  (None = skip mission)
resolve_offsets(meta)
  → (ref_camera, {cam: (offset_s, "gyro"|"sync"|"missing")})
                                    per camera: gyro_offsets_s preferred, sync_offsets_s fallback

find_main_video(mission, cam)     → {cam}.MP4 or GX*_{cam}.MP4
find_lrv_video(mission, cam)      → {cam}_LRV.MP4 / GL*_{cam}_LRV.MP4 / GL*_{cam}.LRV

_window_for(start_ref, end_ref, offset, src_dur)
  → (start, dur, warnings)        ← applies offset, clamps to footage bounds

ffmpeg_crop(src, dst, start, dur, dry_run, reencode)
  → bool                          ← -ss before -i; stream copy (+copy_unknown) or -c:v libx265

crop_mission(...)                 → dry-run prints plan; execute crops + archives
```

**Offset/timing model:** `offset[C]` = seconds camera C started AFTER the reference (same convention as `sync_gyro.py`). For a window `[start_ref, end_ref]` on the reference timeline, camera C is cropped at `start_ref - offset[C]` for the same real-world moments. Negative offset (C started before ref) → C is cropped *later*. The reference camera is the one with offset 0.0.

**Offset source priority (per the user's requirement):** `gyro_offsets_s` first (sub-frame accurate, from gyro cross-correlation), then `sync_offsets_s` (creation-time, second-resolution). A camera with neither is skipped.

**Clamping:** If `start_C < 0` the clip is clamped to footage start (aligns at END, warns). If `end_C > camera_duration` it clamps to footage end (aligns at START, warns). Duration is recomputed after clamping.

**Crop method:** Default is stream copy (`-c copy`) — fast, but the cut lands on the nearest keyframe at/before the start (sub-second imprecision per camera). `--reencode` re-encodes video with libx265 for frame-accurate cuts while copying audio. `-ss` is placed before `-i` for fast input seeking in both modes.

**Stream mapping (important):** crop uses `-map 0:v:0 -map 0:a?` — video + optional audio only. It deliberately does **not** copy the GoPro data streams. GoPro MP4s carry a `tmcd` timecode stream (codec shows as "unknown") and the raw GX files also carry a `gpmd` GPMF stream. Using `-map 0 -copy_unknown` makes the MP4 muxer fail with *"Could not find tag for codec none in stream #2"* because it can't remux the unknown-codec `tmcd` track. Dropping the data streams is the same thing `compact_missions.py` does by omitting `-map 0`. Telemetry is already extracted to `data/` CSVs, so cropped viewing clips don't need GPMF. (The output still gets a valid `tmcd` track that the muxer regenerates from metadata — that's fine, it's not a copy of the broken stream.)

**Archival flow (execute):** Crops to `{name}.cropping{ext}` temp (extension preserved so ffmpeg infers the format) → `shutil.move` original into `raw/` → `os.replace` temp into final name. This ensures the original is preserved before the final name is taken. Pre-flight aborts the mission if a to-be-moved original would collide in `raw/` (unless `--force`). Writes `.gopro_mission` into `raw/` if absent.

**Crop record / re-cropping (`crop.yaml`):** On execute, the script writes `{mission}/crop.yaml` recording the reference camera, window (`start_s`/`end_s`/`duration_s` + HMS), method, and per-output details: `output`, `original` (path in `raw/`), `offset_s`, `offset_src`, `crop_start_s`, `crop_dur_s`.

This file is both a provenance record and the enabler of re-cropping, and `crop_mission` **self-gates** on it (so the script is idempotent — `run_pipeline.py` just always invokes it and lets it decide, the same as every other step keys off its last artifact):
- **First crop** (no `crop.yaml`): each output's source is resolved in the mission folder via `find_main_video`/`find_lrv_video`, cropped, and the original moved to `raw/` (`needs_move=True`).
- **Already done** (`crop.yaml` present, **same** window, LRV already satisfied): the mission is skipped with a message — no footage is recut. Window equality is compared against `crop.yaml`'s `window.start_s/end_s` with a 0.01 s tolerance.
- **Re-crop a different window** (`crop.yaml` present, window differs): **refused unless `--force`**. This is a deliberate guard against silently recutting footage; the message tells you the recorded window and to pass `--force`. With `--force`, each recorded output re-cuts from its recorded `original` in `raw/` (`needs_move=False`; never from the previous crop) and `crop.yaml` is rewritten with the new window.
- **Add LRV** (`crop.yaml` has `lrv: false`, same window, `--lrv` now): **allowed without `--force`**. Only the missing LRV outputs are cropped (first-crop path: source from the mission folder, moved to `raw/`); the already-done MP4 outputs are **not** recut — their records are *carried forward* unchanged. `crop.yaml` is rewritten merging the carried records with the new LRV ones, and `lrv` flips to `true` (the flag is derived from whether any recorded output is an LRV, not from the requested flag).

Driving re-crop from `crop.yaml`'s recorded `original` paths — rather than re-running the finders on `raw/` — is deliberate: `raw/` also contains the chapter files left by `compact_missions.py` (`raw/GX010148_Front.MP4`), so a finder pointed at `raw/` would wrongly pick a chapter. The recorded path is unambiguous.

**Metadata is never modified** — `metadata.json` keeps describing the originals (now in `raw/`), which stays correct since re-crop always cuts from those originals using the same offsets.

**Note:** Missions cropped before `crop.yaml` support existed have no record; re-running treats them as a first crop and the `raw/` collision guard blocks it (the original is already in `raw/`). Backfill a `crop.yaml` by hand (map each `output` to its `original` in `raw/`) to re-enable re-cropping.

## overlay_stats.py architecture

```
load_config(config_path, mission_dir)
  → validated cfg dict with "bag_path" resolved

build_specs(cfg)
  → list[OverlaySpec]           ← one per overlay entry in YAML

resolve_positions(specs, W, H, line_height)
  → mutates specs in-place      ← assigns x, y, an (ASS numpad alignment)

load_bag_topics(bag_path, {topic: [field, ...]})
  → {topic: {"t_s": ndarray, field: ndarray, ...}}
    timestamps normalized to start at 0 (header.stamp preferred)

generate_ass(out_path, specs, topic_data, fps, ...)
  → writes .ass file            ← run-length encodes identical consecutive values
```

**Config file format** (`overlays.yaml` in each mission folder — normally generated by `run_pipeline.py` from the `overlay:` template in `pipeline.yaml`; see the run_pipeline section):

```yaml
bag: plane_2.0-2026-06-11-12-59-10   # relative to mission dir or absolute
bag_offset_s: 150.045                 # seconds bag started AFTER GoPro t=0
camera: Front                         # drives video probe for PlayRes
font_size: 40                         # use 40+ for 5K video, 16-20 for LRV
line_height: 50

overlays:
  - topic: /bluerov2/imu/data
    field: angular_velocity.x         # dot-notation; supports field[0] array indexing
    label: "Gyro X"
    unit: "rad/s"
    format: ".3f"
    position: top-left                # or top-right/bottom-left/bottom-right/top-center/bottom-center
    color: "#00FF00"
    enabled: true                     # optional, default true
```

**Positions:** Named anchors (`top-left`, `top-right`, `bottom-left`, `bottom-right`, `top-center`, `bottom-center`) or `[x, y]` pixel list. Multiple overlays at the same anchor stack vertically. ASS `\an` numpad alignment is set automatically so right-anchored text right-justifies without needing to estimate text width.

**ASS color format:** `&H00BBGGRR&` — R and B are swapped from HTML `#RRGGBB`. Alpha 0x00 = opaque (first byte). The script converts automatically via `_html_to_ass()`.

**Timing:** `t_lookup = t_video - bag_offset_s`. If `t_lookup < 0` (bag hasn't started yet) or `> bag_duration`, the overlay shows "N/A". Uses `np.interp` for interpolation between bag samples.

**Run-length encoding:** Consecutive frames with the same formatted value extend the previous Dialogue event rather than creating a new one. For slow-changing fields this drastically reduces file size (the Plane mission N/A period generates just 5 events instead of ~3600).

**Subclip support (`--start` / `--end`):** All Dialogue timestamps are shifted by `-start_s` so the ASS file aligns with a video that has been trimmed to `[start_s, end_s]`. Use this against the *full* video (before cropping) — it probes the full duration and clamps `--end` to it.

**Cropped-video support (`--crop-offset T`):** For a video already cropped by `crop_missions.py`, the clip begins `T` seconds into the original reference timeline (where `T` = the crop's `--start`). The bag's `t=0` therefore sits at `bag_offset_s` on the original timeline, so the effective offset for the cropped clip is `bag_offset_s - T`. The flag simply subtracts `T` from `bag_offset_s`; no display shift is applied since the cropped file already starts at its own `t=0`. This is the correct way to overlay onto a cropped clip — `--start`/`--end` would mis-clamp because the probed duration is now the short clip. `crop_missions.py` prints the exact `--crop-offset` command to run after a crop.

**PlayRes:** Probed from the actual video at runtime. Set `font_size` accordingly — 40pt at 5K (5312×2988) is visually equivalent to ~6pt at 540p (LRV). To target the LRV proxy, change `camera` to `Front_LRV` or similar and adjust `font_size`.

## extract_ros_imu.py architecture

```
extract_imu(bag_path, topic, out_dir)
  → writes {name}_gyro.csv and {name}_accl.csv

Uses rosbags.highlevel.AnyReader — auto-detects format from path:
  - ROS1 .bag file  → pass the .bag file path
  - ROS2 multi-file bag (directory of .mcap + metadata.yaml) → pass the directory path
```

**ROS timestamp vs IMU header stamp — always use the header stamp:**

The `rosbags` iterator yields `(connection, timestamp_ns, rawdata)`. The `timestamp_ns` is the **bag record time** — when the OS received and logged the message. On the Plane dataset this lagged the sensor by 4–25 ms with ~3.5 ms stdev (OS scheduling jitter). It is not suitable for precision sync.

Always use `msg.header.stamp` instead:
```python
stamp = msg.header.stamp
t_s = stamp.sec + stamp.nanosec * 1e-9
```

**Units — ROS matches GoPro, no conversion needed:**
- `angular_velocity` (gyro): rad/s — same as GoPro GYRO stream
- `linear_acceleration` (accel): m/s² — same as GoPro ACCL stream

**Apparent Hz display artifact in sync_gyro.py:**

`sync_gyro.py` reports Hz as `1 / median(diff(t))`. ROS IMU messages can be bursty (bursts with sub-millisecond gaps between messages in the same burst), making the median inter-sample gap smaller than the true period. The Plane bluerov2 shows ~349 Hz in the display but the actual average rate is 392 693 samples / 1964 s ≈ 200 Hz. The cross-correlation is unaffected because both signals are resampled to a uniform 5 ms grid before correlating.

**One bag per mission — invariant:**

Each mission folder contains exactly one ROS bag directory. `extract_ros_imu.py` discovers bags by scanning for `metadata.yaml` (ROS2) or `.bag` files (ROS1) and writes output into `{bag_parent}/data/`. This works correctly only when each mission has one bag; multiple bags per mission would overwrite each other's CSVs.

**Integrating a ROS bag with sync_gyro.py:**

1. Run `extract_ros_imu.py /path/to/root` to extract all missions at once — it writes CSVs directly into each mission's `data/` folder.
2. `sync_gyro.py` globs `*_gyro.csv` in `data/` — no changes needed; the new source is included automatically.
3. Set `--max-lag` large enough to cover the expected offset between the bag start and the GoPro reference camera start. On the Plane mission the bag started ~150 s after the GoPro Front camera, so `--max-lag 200` is the minimum safe value.
4. Pairwise consistency between the ROS source and each GoPro camera is printed automatically when ≥3 sources are present.

**Plane mission bag details (confirmed 2026-06-11):**
- Bag: `plane_2.0-2026-06-11-12-59-10/` (ROS2 multi-file mcap, 17 segments)
- IMU topic: `/bluerov2/imu/data` (`sensor_msgs/msg/Imu`, 200 Hz)
- Bag start epoch: 1781204353.465 s (header stamp); GoPro Front start: 1781204211.0 s
- Cross-correlation result: offset = +150 045 ms after Front, r = 0.8385

## run_pipeline.py architecture

```
load_config(root, config_path, args)
  → cfg dict        ← defaults < pipeline.yaml < CLI overrides (CLI > per-mission YAML > global)

find_missions(root) / unorganized_camera_dirs(root) / find_bags(mission)
  → discovery       ← find_bags is replicated inline so importing rosbags is NOT required

mission_state(mission, cfg)
  → dict[step,bool] ← per-mission done-detection, mirroring each child's own skip/force guards

generate_crop_plan / read_crop_plan
  → crop_plan.csv   ← checkpoint-2 file (uses crop_missions.load_metadata + resolve_offsets)

drive(root, cfg, selected, execute, force)
  → runs each selected step in order, pausing at the 2 checkpoints
run(cmd, label)     → subprocess.run(sys.executable, script, ...) ; streams output, checks exit code
```

**Design choices:**
- **Subprocess, not import.** Each step is run as `python <script>.py <root> <flags>`. The existing scripts already iterate a root via their own `find_missions`, print good plans, and validate their own args; subprocess keeps them canonical and isolates optional deps. The only things imported from siblings are read-only helpers: `utils.MISSION_MARKER`, `crop_missions.load_metadata`, `crop_missions.resolve_offsets`. `extract_ros_imu.find_bags` is **re-implemented inline** because that module imports `rosbags` at top level.
- **Dry-run/execute normalization.** Orchestrator is dry-run by default. `--execute` translates per step because the children disagree: organize/compact/crop need `--execute`; telemetry/sync/overlay take `--dry-run`; ros always acts (skipped entirely in dry-run).
- **Step granularity.** organize/compact/telemetry/ros run once at the **root** (children loop internally and self-skip done missions). sync/crop/overlay run **per-mission**: sync to honor per-mission `max_lag_s` overrides, crop because each mission has its own window, overlay because each mission's `overlays.yaml` is generated/rendered individually.
- **Overlay config is generated, not hand-written per mission.** The `overlay:` block in `pipeline.yaml` (`font_size`, `line_height`, `overlays:` list) is the single template. Before rendering, `drive()` calls `generate_overlays_yaml(mission, cfg, force)` for each mission, which writes `{mission}/overlays.yaml` auto-filling: `bag` (first hit from `find_bags`, path relative to the mission), `camera` (reference camera from `resolve_offsets`), and `bag_offset_s` (`gyro_offsets_s[<bag source>]` from `metadata.json`, falling back to `sync_offsets_s`, then the template's `bag_offset_s`/0.0). `<bag source>` is `topic_name(cfg["ros"]["topic"])` (e.g. `bluerov2`) — the same stem `extract_ros_imu.py`/`sync_gyro.py` key the bag's offset under, so the auto-filled `bag_offset_s` equals the measured offset. The template's `font_size`/`line_height`/`overlays` are copied verbatim. An existing `overlays.yaml` is **kept** (your hand edits survive) unless `--force` regenerates it; a mission with no bag or an empty template is skipped (then rendered only if a hand-written `overlays.yaml` already exists). Per-mission template overrides live under `missions.<name>.overlay` (merged over the global `overlay` block by `resolve_overlay_cfg`).
- **Two checkpoints halt the run.** `unorganized_camera_dirs(root)` non-empty → export `mission_plan.csv` and stop. After sync, no `crop_plan.csv` → generate it (pre-filled reference camera/duration/offset-source per mission) and stop. Re-running after each edit resumes.
- **Crop gating lives in `crop_missions.py`, not the orchestrator.** `drive()` does **not** pre-check `crop.yaml`; it just invokes `crop_missions.py` per filled `crop_plan.csv` row and lets the child self-gate (skip same-window, add LRV to an `lrv:false` crop, require `--force` for a different window — see the crop section). The orchestrator's own `--force` is forwarded through. `crop_lrv_flag`/`crop_done` are kept only so `--status`/`mission_state` can report crop done-ness (done = `crop.yaml` exists **and** LRV is satisfied when `cfg["lrv"]`) without shelling out.
- **LRV is a global choice, not per-mission.** `crop_plan.csv` has **no `lrv` column** — LRV comes only from `cfg["lrv"]` (`--lrv` flag or `pipeline.yaml: lrv`). `reencode` is still per-mission in the plan (OR'd with the global flag).
- **Selection:** `--steps a,b` / `--only x` / `--from x --to y` / `--skip a,b` against the fixed order `organize,compact,telemetry,ros,sync,crop,overlay`. `--status` prints a missions × steps matrix (`✓` done, `·` pending, `—` not applicable). `step_na` flags `—`: `ros` when the mission has no bag; `sync` when there are fewer than 2 gyro sources to cross-correlate (single-camera, no-bag mission), decided once `metadata.json` exists. `overlay` is never N/A — it stays `·` pending until a `{camera}_stats.ass` is generated, since you can always add an `overlays.yaml`.
- **Config:** optional `pipeline.yaml` at root holds global + per-mission overrides (e.g. `missions.Plane.sync.max_lag_s: 200`); CLI flags (`--lrv`, `--reencode`, `--no-plots`, `--max-lag`, `--dt`, `--ros-topic`) override it.

Generated artifacts at the root: `mission_plan.csv` (checkpoint 1), `crop_plan.csv` (checkpoint 2), optional `pipeline.yaml`.

## Test data

`/media/bjm255/Frostlab/SandHollow` — already organized by `organize_missions.py` and compacted by `compact_missions.py`. Missions: Ball, BlueBoat, Dam, DiveArea, FlatOpen, Plane, other.

- Ball and other already have `data/` folders from `extract_telemetry.py` runs — use `--force` to re-extract.
- **other** and **Ball** are the best missions for quick testing (smallest files: other has clips under 32 MB; Ball has single-chapter GX files without the multi-chapter complexity of DiveArea/Dam).
- All cameras are HERO11 Black with GPS disabled — no GPS5 stream will appear in any file from this dataset.
- **Plane** has a ROS2 bag (`plane_2.0-2026-06-11-12-59-10/`) with a `/bluerov2/imu/data` topic. `bluerov2_gyro.csv` and `bluerov2_accl.csv` have already been extracted into `Plane/data/`. Use `--max-lag 200` when running `sync_gyro.py` on this mission.
- **Multiple missions carry ROS2 bags** (confirmed 2026-06-15): Ball, Dam, DiveArea, FlatOpen, and Plane each have a bag with `bluerov2_gyro.csv` already extracted into their `data/`. BlueBoat and other have no bag. `run_pipeline.py --status` shows `—` in the `ros` column for the bag-less missions.
