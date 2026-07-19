"""Static configuration constants shared across the core layer.

Deliberately Qt-free and hardware-free so the whole core package stays unit
testable. UI-persisted settings (last working folder, etc.) live in the Qt layer
via QSettings; this module only holds fixed structural constants.
"""

from __future__ import annotations

from enum import Enum


class MediaType(str, Enum):
    """The three pipeline media types.

    The ``value`` is what gets written to the catalog's ``media_type`` column.
    """

    FLOPPY = "floppy"
    HDD = "hdd"
    OPTICAL = "disc"

    @property
    def folder_name(self) -> str:
        """Top-level subfolder inside the working folder for this media type."""
        return {
            MediaType.FLOPPY: "Floppy",
            MediaType.HDD: "HDD",
            MediaType.OPTICAL: "CD",
        }[self]

    @property
    def tmp_name(self) -> str:
        """Segment used under ``.tmp/`` for this pipeline's staging dirs."""
        return {
            MediaType.FLOPPY: "floppy",
            MediaType.HDD: "hdd",
            MediaType.OPTICAL: "optical",
        }[self]

    @property
    def fallback_prefix(self) -> str:
        """Prefix used in the auto-generated fallback name ``{prefix}_{NNN}_{date}``."""
        return {
            MediaType.FLOPPY: "floppy",
            MediaType.HDD: "drive",
            MediaType.OPTICAL: "disc",
        }[self]


# --- Working-folder layout -------------------------------------------------

CATALOG_FILENAME = "catalog.csv"
TMP_DIRNAME = ".tmp"
EXTRACTED_DIRNAME = "Extracted Files"

# Per-item artifact filename patterns (``{name}`` = resolved chosen_name).
RAW_IMAGE_SUFFIX = ".img"
COMPRESSED_IMAGE_SUFFIX = ".img.zst"
LOG_SUFFIX = ".log"
PHOTO_SUFFIX = "_photo.jpg"


# --- Compression -----------------------------------------------------------

# zstd, level 19, --long window, all cores. Shelled out to the `zstd` CLI.
ZSTD_LEVEL = 19
ZSTD_LONG = True
ZSTD_THREADS = 0  # 0 == use all available cores (-T0)


# --- Date sanity bounds ----------------------------------------------------

# Volume modification dates outside [MIN_YEAR, today] are treated as suspect —
# commonly a dead CMOS battery resetting the clock. See datescan.py.
MIN_VALID_YEAR = 1980


# --- Filesystem detection --------------------------------------------------

# Candidate filesystem types tried, in order, for a loopback mount when blkid /
# signature detection is inconclusive. Ordered most-likely-first for old media.
CANDIDATE_MOUNT_FSTYPES = ("vfat", "msdos", "ntfs", "ext2")

# FAT-family filesystem identifiers that should be extracted with mtools rather
# than a kernel mount.
FAT_FSTYPES = ("vfat", "msdos", "fat", "fat12", "fat16", "fat32")


# --- Catalog status values -------------------------------------------------

class Status(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    UNRECOGNIZED_FS = "unrecognized_fs"


# Sentinel written to the detected-filesystem column when nothing recognized it.
UNRECOGNIZED_FS_LABEL = "unrecognized_filesystem"
