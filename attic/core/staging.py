"""Per-job ``.tmp/`` staging and the move into the final layout.

This is the filesystem correctness guarantee for concurrency: every capture job
gets its own isolated ``.tmp/{pipeline_type}/{session_id}/`` directory, so the
three pipelines — and concurrent jobs of the same type — never share a temp path
and cannot step on each other. Only after BOTH compression and extraction
succeed is a job's content moved to its final Floppy/HDD/CD location and the
temp directory removed. On failure the temp directory is left in place for
inspection and its path recorded in the catalog.

Staging lives under a ``staging_root`` that is independent of the working
folder holding the final archive (see ``AppSettings.staging_root``) -- typically
a fast local disk, while the archive itself may be on a separate, possibly
slower or removable drive. When both happen to be the same filesystem the move
is a plain atomic ``os.replace``; when they differ, :func:`promote` falls back
to a copy + cheap verify + delete.
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass
from datetime import datetime

from .config import MediaType, TMP_DIRNAME


def new_session_id(now: datetime | None = None) -> str:
    """A timestamp-based id unique to a capture job (``YYYYmmdd_HHMMSS_ffffff``).

    Microseconds are included so two jobs of the same type starting within the
    same second still get distinct directories.
    """
    now = now or datetime.now()
    return now.strftime("%Y%m%d_%H%M%S_%f")


@dataclass
class StagingDir:
    """An allocated per-job temp directory under the staging root's ``.tmp/``."""

    staging_root: str
    media_type: MediaType
    session_id: str

    @property
    def path(self) -> str:
        return os.path.join(
            self.staging_root, TMP_DIRNAME, self.media_type.tmp_name, self.session_id
        )

    def child(self, *parts: str) -> str:
        return os.path.join(self.path, *parts)

    def exists(self) -> bool:
        return os.path.isdir(self.path)


def create_staging(
    staging_root: str, media_type: MediaType, session_id: str | None = None
) -> StagingDir:
    """Create and return an isolated staging directory for one job."""
    session_id = session_id or new_session_id()
    staging = StagingDir(staging_root, media_type, session_id)
    os.makedirs(staging.path, exist_ok=False)
    return staging


def final_dir(working_folder: str, media_type: MediaType, chosen_name: str) -> str:
    """Final destination directory for a completed job, e.g. ``<wf>/CD/<name>``."""
    return os.path.join(working_folder, media_type.folder_name, chosen_name)


def promote(staging: StagingDir, dest_dir: str) -> str:
    """Move a completed staging dir to ``dest_dir``, then clean up.

    Uses ``os.replace`` when the staging root and the archive share a
    filesystem -- atomic, and the common case. When they don't (staging root
    and working folder configured on separate drives), falls back to a copy +
    cheap verify + delete (see :func:`_promote_cross_device`). The parent of
    ``dest_dir`` is created first. ``dest_dir`` must not already exist (name
    dedup happens upstream in naming). Returns ``dest_dir``.
    """
    if os.path.exists(dest_dir):
        raise FileExistsError(f"destination already exists: {dest_dir}")
    os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
    try:
        os.replace(staging.path, dest_dir)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _promote_cross_device(staging.path, dest_dir)
    _prune_empty_tmp_parents(staging)
    return dest_dir


def _promote_cross_device(src: str, dest_dir: str) -> None:
    """Copy + verify + delete fallback for :func:`promote` across filesystems.

    Verification compares total file count and byte count between source and
    destination rather than re-reading file contents: a full recursive
    read/diff pass over a freshly copied tree of hundreds of thousands of small
    files (e.g. a whole recovered NTFS partition) has been measured to take
    30-60+ minutes over a FUSE-mounted destination, which defeats the point of
    decoupling staging from the archive. Size/count mismatches still catch a
    genuinely incomplete or corrupted copy.
    """
    shutil.copytree(src, dest_dir, symlinks=True)
    src_stats = _tree_stats(src)
    dst_stats = _tree_stats(dest_dir)
    if src_stats != dst_stats:
        shutil.rmtree(dest_dir, ignore_errors=True)
        src_count, src_bytes = src_stats
        dst_count, dst_bytes = dst_stats
        raise OSError(
            "cross-device promote verification failed: source had "
            f"{src_count} files/{src_bytes} bytes, copy has "
            f"{dst_count} files/{dst_bytes} bytes"
        )
    shutil.rmtree(src)


def _tree_stats(root: str) -> tuple[int, int]:
    """(file count, total byte size) for every regular file/symlink under root."""
    count = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
            count += 1
    return count, total


def _prune_empty_tmp_parents(staging: StagingDir) -> None:
    """Remove now-empty ``.tmp/{type}`` and ``.tmp`` dirs, best effort.

    Never touches non-empty directories (other concurrent jobs may still be
    staging there), so this is safe to call from any finishing job.
    """
    type_dir = os.path.join(staging.staging_root, TMP_DIRNAME, staging.media_type.tmp_name)
    tmp_root = os.path.join(staging.staging_root, TMP_DIRNAME)
    for d in (type_dir, tmp_root):
        try:
            os.rmdir(d)  # only succeeds when empty
        except OSError:
            break  # non-empty or missing — stop pruning upward


def discard(staging: StagingDir) -> None:
    """Delete a staging dir outright (e.g. user abandons a failed job)."""
    shutil.rmtree(staging.path, ignore_errors=True)
    _prune_empty_tmp_parents(staging)
