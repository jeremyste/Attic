"""Per-working-folder settings, persisted as JSON inside the folder itself.

Settings travel with the working folder (not the machine), so resuming a folder —
possibly on another workstation — keeps the same choices. Qt-free and testable;
the Qt settings dialog reads/writes through here.

Unknown keys in the on-disk file are ignored and missing keys fall back to
defaults, so the file format tolerates version drift in both directions.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

from .config import ZSTD_LEVEL, ZSTD_LONG

SETTINGS_FILENAME = "attic_settings.json"


@dataclass
class AppSettings:
    # Compression
    zstd_level: int = ZSTD_LEVEL
    zstd_long: bool = ZSTD_LONG
    keep_raw_image: bool = False  # keep the uncompressed .img alongside the .zst

    # Devices
    optical_device: str = "/dev/sr0"
    ddrescue_retries: int = 3

    # Floppy geometry (the track-grid dimensions)
    floppy_cylinders: int = 80
    floppy_heads: int = 2

    # Webcam
    camera_index: int = 0
    skip_photo: bool = False  # never prompt for a photo when True

    # Naming
    # When True, unlabeled volumes silently keep their generated fallback name and
    # are NOT queued in Pending Labels. Leave False if someone will label them.
    auto_accept_fallback_names: bool = False

    # HDD workflow: photograph + label before selecting/docking the drive.
    hdd_photo_before_dock: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def settings_path(working_folder: str) -> str:
    return os.path.join(working_folder, SETTINGS_FILENAME)


def load_settings(working_folder: str) -> AppSettings:
    """Load settings for ``working_folder``, falling back to defaults.

    Missing file, unreadable file, or malformed JSON all yield defaults; known
    keys present in the file override defaults, unknown keys are ignored.
    """
    path = settings_path(working_folder)
    defaults = AppSettings()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    known = {f.name for f in fields(AppSettings)}
    kwargs = {k: v for k, v in data.items() if k in known}
    try:
        return AppSettings(**{**asdict(defaults), **kwargs})
    except TypeError:
        return defaults


def save_settings(working_folder: str, settings: AppSettings) -> str:
    """Write ``settings`` to the working folder. Returns the path written."""
    path = settings_path(working_folder)
    os.makedirs(working_folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(settings.to_json())
    return path
