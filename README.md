# Attic

A PyQt6 desktop application (Linux) for archiving old media — floppy disks,
home-burned optical discs (CD/DVD), and USB-attached hard drives — into
compressed disk images plus expanded file folders, tracked in a single CSV
catalog. Three independent, concurrently-operable pipelines: **Floppy**, **HDD**,
**Optical**.

See `Task.md` for the full specification.

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
Everything for the session goes into that folder: `catalog.csv` plus
`Floppy/`, `HDD/`, `CD/` subtrees.

## Development / tests

The non-GUI core logic (naming, sanitization, catalog, filesystem detection,
compression, staging, device filtering) has no Qt or hardware dependency and is
covered by a pytest suite with mocked subprocesses:

```bash
source .venv/bin/activate
pytest
```
