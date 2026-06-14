# CLAUDE.md — GoPro Mission Tools

Notes for AI-assisted development of the GoPro mission toolchain.

## File layout

```
utils.py                ← shared constants, dataclasses, ffprobe helpers
organize_missions.py    ← step 1: sort raw camera files into mission folders
compact_missions.py     ← step 2: concatenate chapters, archive originals
extract_telemetry.py    ← step 3: extract GPMF telemetry, generate plots & metadata
```

## What each script does

**`organize_missions.py`** — Scans a folder of GoPro camera subfolders, groups recordings across cameras into "missions" by matching start times and durations, produces a reviewable CSV, and moves files into named output folders after human approval.

**`compact_missions.py`** — Scans a folder of already-organized mission subfolders (identified by `.gopro_mission` marker), concatenates chapter files per camera into single videos using lossless stream copy, and moves all originals to a `raw/` subfolder.

**`utils.py`** — Shared code imported by both scripts: `MISSION_MARKER`, `NAME_RE`, `VIDEO_EXTS`, `PROXY_EXTS`, `THUMB_EXTS`, `FileEntry`, `Recording`, `ffprobe()`, `thm_creation()`, `mtime()`, `best_start()`, `total_duration()`. Any future tool in this repo should import from here rather than re-implementing.

**`extract_telemetry.py`** — Reads GPMF binary streams from raw chapter MP4 files (from `raw/` subfolders or main folder if not compacted), extracts telemetry data, and writes results into `{mission}/data/`. Does not modify any video files.

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

## Test data

`/media/bjm255/Frostlab/SandHollow` — already organized by `organize_missions.py` and compacted by `compact_missions.py`. Missions: Ball, BlueBoat, Dam, DiveArea, FlatOpen, Plane, other.

- Ball and other already have `data/` folders from `extract_telemetry.py` runs — use `--force` to re-extract.
- **other** and **Ball** are the best missions for quick testing (smallest files: other has clips under 32 MB; Ball has single-chapter GX files without the multi-chapter complexity of DiveArea/Dam).
- All cameras are HERO11 Black with GPS disabled — no GPS5 stream will appear in any file from this dataset.
