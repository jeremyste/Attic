# Attic

A PyQt6 desktop application (Linux) for archiving old media: floppy disks,
home-burned optical discs (CD/DVD), and USB-attached hard drives. It captures
compressed disk images plus expanded file folders, tracked in a single CSV
catalog. Three independent, concurrently-operable pipelines: **Floppy**, **HDD**,
**Optical**.

## Highlights

- **Three concurrent pipelines.** Floppy, HDD, and Optical each run in their own
  background thread; starting an HDD rescue never freezes the other tabs.
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
Greaseweazle host tools (`gw`, installed from the upstream git repo; it is not
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
./run-attic.sh
```

The launcher checks the environment before starting: Python version, the
virtualenv and its packages, the external CLI tools, and `dialout` group
membership for the Greaseweazle. It offers to install anything missing rather
than failing with a traceback, and is safe to re-run. Or start it directly:

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
      <name>.img.zst       # compressed sector image, decoded from the flux
      <name>.scp.zst       # compressed flux master (fall-back; see below)
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

1. **Photo -> label -> load media -> capture.** Take the item photo(s) and enter the
   physical label *before* inserting/loading the media (a sticker can't be read
   once a disk is in the drive). For HDDs you then dock the drive and pick it from
   a device list; a confirmation restates model/size/path before any read.
2. **Image + detect + extract.** The raw image is captured, its filesystem is
   auto-detected (`blkid` -> `file -s` -> candidate loopback mount), and recognized
   filesystems are expanded into `Extracted Files/`. Unrecognized disks keep the
   raw image only; the pipeline never aborts over one bad disk.
3. **Compress + checksum + catalog.** zstd (level 19, `--long`, all cores) and
   SHA-256 run in a background pool, then the job is atomically moved into place
   and a catalog row is written.

**Begin Capture re-enables as soon as the hardware is free**, not when the job
finishes. The floppy flux read, the optical ddrescue pass and the HDD rescue
passes are the only stages that need the drive; decode, extraction, compression
and cataloguing all continue in the background while you load the next item.
Several jobs can therefore be in flight at once, across all three pipelines.

The **Processing** panel lists every job still being worked on, tagged by
pipeline (`[FLOPPY]`, `[HDD]`, `[CD/DVD]`) with its current stage and a progress
bar. The bar is determinate only where a real measurement exists -- gw's track
count during a read or decode, ddrescue's rescued fraction -- and shows a busy
indicator during extraction and compression, which report no usable total.
Finished jobs linger briefly, then clear.

Turn on **Unattended naming** if you want a capture never to stop and ask: the
typed label wins, else a detected volume label, else a generated name. Disks
with no label at all still appear in Pending Labels to be named later. With it
off, the naming dialog appears after each capture as before -- which will block
the queue while it waits for you.

### Floppy flux capture

By default the floppy pipeline reads the **raw flux** off the disk
(`gw read --raw`), then decodes the sector image from that flux host-side
(`gw convert`) rather than letting gw decode during the read. A finished disk
therefore keeps three layers, most to least convenient:

1. `Extracted Files/` - browse immediately, no tools needed.
2. `<name>.img.zst` - the sector image, for mounting or emulators.
3. `<name>.scp.zst` - the flux master, to re-decode years from now (with better
   tools, or a different format guess) *without* putting fragile media back in
   a drive.

Two practical consequences:

- **The drive is only needed for step one.** Decode, extraction and compression
  are pure host work, so the disk can be ejected and the next one loaded while
  the pipeline is still chewing on the last.
- **A disk that decodes to nothing still archives.** If `gw convert` produces no
  image, the flux is kept on its own and the row is marked `unrecognized_fs`;
  that case is exactly what the flux copy exists for.

Cost is roughly **10-15 MB compressed per 1.44M disk** (about 40x the sector
image), and it is near-constant per disk - flux size tracks transition count,
not how much data the disk holds, so a blank disk costs full price. Turn it off
with the *Preserve flux* setting to read straight to a sector image instead.

### Naming

Name priority is: **physical label entered -> detected volume/partition label ->
generated fallback** `{type}_{NNN}_{date}`. All three sources are recorded in the
catalog even when they disagree. A disk with no physical and no detectable label
is archived immediately under its fallback name and listed in the **Pending
Labels** panel so it can be renamed later (which updates both the folder and the
catalog); or, if you enable *auto-accept generated names*, it's kept silently.

### HDD safety

The device list shows only removable/USB whole disks; internal and
system-mounted disks are excluded. If a genuine target drive in an enclosure
mis-reports as internal, enable **"Show all drives (advanced)"**; such drives are
flagged (!) and require an extra, deliberate confirmation before any read.

## Settings

Reachable via **File -> Settings...**, stored as `attic_settings.json` **inside the
working folder** (so they travel with the archive):

| Setting | Purpose |
|---|---|
| zstd level / `--long` / keep raw `.img` | Compression tuning; optionally retain the uncompressed image |
| Optical device | Default `/dev/srN` |
| ddrescue retries | Rescue aggressiveness |
| Floppy cylinders / heads | Track-grid geometry (e.g. 40-track, single-sided) |
| Floppy disk format | `gw --format` value. Default `ibm.scan` probes the IBM FM/MFM variants per track, covering 160K-1.44M DOS-era disks; set explicitly for other media (`amiga.amigados`, `atarist.720`, ...). `gw read --help` lists them all |
| Greaseweazle port | Serial port override; blank (default) lets `gw` auto-detect |
| Preserve flux | On by default. Archive a `.scp` flux master and decode the image from it; see [Floppy flux capture](#floppy-flux-capture) |
| Flux revolutions | Revolutions per track for the flux read. "format default" (2 for `ibm.scan`) unless raised; each extra revolution adds ~50% to the flux size but gives a damaged track another independent sample |
| Camera index / skip photo | Webcam selection; disable photo prompts |
| Unattended naming | Never interrupt a capture to confirm a name; unlabeled disks skip the Pending Labels queue too |
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
- The `gw` invocation has been exercised against a real Greaseweazle V4.1 (host
  tools 1.23, firmware 1.6) with an empty drive: the command is accepted, reaches
  the drive, and the no-disk condition is correctly reported as a failed capture.
- The flux decode chain (`gw convert` -> detect -> extract -> compress -> catalog)
  is verified end-to-end against a real 57 MB flux stream, with only the drive
  read itself stubbed. What remains unverified on hardware is the read stage:
  `gw read --raw` against a physical disk, and the live track grid it feeds.
- `ddrescue` against real devices and the live webcam path require hardware and
  have not been exercised here; verify against your own rig on first use.
- Note that `gw read` exits 0 even when the read fails at the device level (it
  catches the error, prints `Command Failed: ...`, and writes no image). The
  floppy controller therefore checks gw's output and the resulting file rather
  than trusting the exit status.
- Per-partition HDD folders are auto-named from detected labels; there is no
  interactive per-partition naming dialog yet.

## License

Released under the GNU General Public License, version 3 (GPLv3). See
<https://www.gnu.org/licenses/gpl-3.0.html> for the full license text.
