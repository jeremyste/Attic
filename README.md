# Attic

A PyQt6 desktop application (Linux) for archiving old media — floppy disks,
home-burned optical discs (CD/DVD), and USB-attached hard drives — into
compressed disk images plus expanded file folders, tracked in a single CSV
catalog. Three independent, concurrently-operable pipelines: **Floppy**, **HDD**,
**Optical**.

## Highlights

- **Three concurrent pipelines.** Floppy, HDD, and Optical each run in their own
  background thread — starting an HDD rescue never freezes the other tabs.
- **Faithful imaging + expansion.** Every disk is imaged (`gw` for floppies,
  `ddrescue` for HDD/optical), compressed to `.zst`, checksummed, *and* its
  filesystem expanded into a browsable `Extracted Files/` folder.
- **One CSV catalog** at the working-folder root, one row per volume (one row per
  partition for multi-partition drives), recording every label source, sizes,
  SHA-256s, filesystem, and status.
- **Webcam item photos** with classical-CV auto-crop (front + back for floppies
  and drives, single shot for discs), captured at the camera's highest resolution.
- **Safety first for HDDs.** Only removable/USB disks are offered by default; the
  system/boot disk is filtered out, with an explicit, warned override for drives
  that mis-report as internal.

## Requirements

### System CLI tools (not pip-installable)

These are shelled out to directly and must be present on `PATH`:

```bash
sudo apt install -y gddrescue mtools parted util-linux file zstd coreutils policykit-1 \
                    libgl1 libglib2.0-0 libxcb-cursor0 gcc python3-dev
```

- `gddrescue` provides `ddrescue`; `util-linux` provides `blkid`/`lsblk`/`sfdisk`;
  `coreutils` provides `sha256sum`; `policykit-1` provides `pkexec`.
- `libgl1`, `libglib2.0-0`, `libxcb-cursor0` are runtime libs the OpenCV and
  PyQt6 wheels need; `gcc`/`python3-dev` are only needed to build Greaseweazle.

### Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` installs `PyQt6`, `opencv-python`, `pytest`, and the
Greaseweazle host tools (`gw`, installed from the upstream git repo — it is not
published on PyPI).

### Privileges

The GUI runs as your normal user. Privileged operations (`ddrescue` against raw
block devices, `mount`/`umount`) are wrapped with `pkexec`, which prompts for
authentication when needed. Do **not** run the whole app as root.

### Hardware (per pipeline, only when actually capturing)

- **Floppy:** a Greaseweazle board + floppy drive. For device access install the
  udev rules: copy `49-greaseweazle.rules` (from a Greaseweazle release) into
  `/etc/udev/rules.d/`.
- **HDD:** the target drive attached via USB (adapter or enclosure).
- **Optical:** an optical drive (e.g. `/dev/sr0`).
- **All:** a webcam for the item photo (optional; capture can be skipped).

## Running

```bash
source .venv/bin/activate
python main.py
```

On launch you pick a destination working folder (the last one is remembered).
Everything for the session goes into that one folder.

### Working-folder layout

```
<working folder>/
  catalog.csv              # the single catalog, one row per volume/partition
  attic_settings.json      # per-folder settings (travels with the archive)
  Floppy/<name>/
      <name>.img.zst       # compressed raw image
      <name>.log           # gw logfile (kept uncompressed)
      <name>_photo_front.jpg
      <name>_photo_back.jpg
      Extracted Files/
  HDD/<name>/
      <name>.img.zst
      <name>.log
      <name>_photo_front.jpg / _back.jpg
      Partition 1 - <label>/   # or a single "Extracted Files/" if one partition
      Partition 2 - <label>/
  CD/<name>/
      <name>.img.zst
      <name>.log
      <name>_photo.jpg
      Extracted Files/
  .tmp/<type>/<session_id>/  # per-job staging; promoted atomically on success
```

## Workflow

Each pipeline follows the same shape:

1. **Photo → label → load media → capture.** Take the item photo(s) and enter the
   physical label *before* inserting/loading the media (a sticker can't be read
   once a disk is in the drive). For HDDs you then dock the drive and pick it from
   a device list; a confirmation restates model/size/path before any read.
2. **Image + detect + extract.** The raw image is captured, its filesystem is
   auto-detected (`blkid` → `file -s` → candidate loopback mount), and recognized
   filesystems are expanded into `Extracted Files/`. Unrecognized disks keep the
   raw image only — the pipeline never aborts over one bad disk.
3. **Compress + checksum + catalog.** zstd (level 19, `--long`, all cores) and
   SHA-256 run in a background pool, then the job is atomically moved into place
   and a catalog row is written.

### Naming

Name priority is: **physical label entered → detected volume/partition label →
generated fallback** `{type}_{NNN}_{date}`. All three sources are recorded in the
catalog even when they disagree. A disk with no physical and no detectable label
is archived immediately under its fallback name and listed in the **Pending
Labels** panel so it can be renamed later (which updates both the folder and the
catalog) — or, if you enable *auto-accept generated names*, it's kept silently.

### HDD safety

The device list shows only removable/USB whole disks; internal and
system-mounted disks are excluded. If a genuine target drive in an enclosure
mis-reports as internal, enable **"Show all drives (advanced)"** — such drives are
flagged ⚠ and require an extra, deliberate confirmation before any read.

## Settings

Reachable via **File → Settings…**, stored as `attic_settings.json` **inside the
working folder** (so they travel with the archive):

| Setting | Purpose |
|---|---|
| zstd level / `--long` / keep raw `.img` | Compression tuning; optionally retain the uncompressed image |
| Optical device | Default `/dev/srN` |
| ddrescue retries | Rescue aggressiveness |
| Floppy cylinders / heads | Track-grid geometry (e.g. 40-track, single-sided) |
| Camera index / skip photo | Webcam selection; disable photo prompts |
| Auto-accept generated names | Skip Pending Labels for unlabeled volumes |
| HDD photo-before-dock | Photograph & label before selecting the drive |

## Architecture

Strict separation keeps all correctness logic testable without Qt or hardware:

```
attic/core/         pure + subprocess logic, no Qt (naming, sanitize, catalog,
                    fsdetect, compression, staging, devices, ddrescue, partition,
                    settings)
attic/controllers/  per-pipeline QThread capture workers + a shared finalize pool
attic/ui/           thin Qt layer: tabs, dialogs, widgets, webcam, orchestration
```

External tools are always shelled out to (never reimplemented), through a single
`subprocess_util.run` wrapper that tests monkeypatch.

## Development / tests

The core logic has no Qt or hardware dependency and is covered by a pytest suite
with mocked subprocesses; GUI-adjacent tests run headless (`offscreen`):

```bash
source .venv/bin/activate
QT_QPA_PLATFORM=offscreen pytest
```

## Status & limitations

- The non-GUI core is thoroughly unit-tested; the full app constructs and the
  finalize path is verified end-to-end with real `zstd`/`sha256sum`.
- The actual capture steps (`gw read`, `ddrescue` against real devices, live
  webcam) require hardware and have not been exercised here — verify against your
  own rig on first use. Tool flags for `gw`/`ddrescue` are kept conservative and
  cross-version-safe; check them against your installed versions.
- Per-partition HDD folders are auto-named from detected labels; there is no
  interactive per-partition naming dialog yet.
