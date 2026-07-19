"""Per-job ``.tmp/`` staging and the atomic move into the final layout.

This is the filesystem correctness guarantee for concurrency: every capture job
gets its own isolated ``.tmp/{pipeline_type}/{session_id}/`` directory, so the
three pipelines — and concurrent jobs of the same type — never share a temp path
and cannot step on each other. Only after BOTH compression and extraction
succeed is a job's content atomically moved to its final Floppy/HDD/CD location
and the temp directory removed. On failure the temp directory is left in place
for inspection and its path recorded in the catalog.
"""

from __future__ import annotations

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
    """An allocated per-job temp directory under the working folder's ``.tmp/``."""

    working_folder: str
    media_type: MediaType
    session_id: str

    @property
    def path(self) -> str:
        return os.path.join(
            self.working_folder, TMP_DIRNAME, self.media_type.tmp_name, self.session_id
        )

    def rel_path(self) -> str:
        """Path relative to the working folder (for catalog notes)."""
        return os.path.relpath(self.path, self.working_folder)

    def child(self, *parts: str) -> str:
        return os.path.join(self.path, *parts)

    def exists(self) -> bool:
        return os.path.isdir(self.path)


def create_staging(
    working_folder: str, media_type: MediaType, session_id: str | None = None
) -> StagingDir:
    """Create and return an isolated staging directory for one job."""
    session_id = session_id or new_session_id()
    staging = StagingDir(working_folder, media_type, session_id)
    os.makedirs(staging.path, exist_ok=False)
    return staging


def final_dir(working_folder: str, media_type: MediaType, chosen_name: str) -> str:
    """Final destination directory for a completed job, e.g. ``<wf>/CD/<name>``."""
    return os.path.join(working_folder, media_type.folder_name, chosen_name)


def promote(staging: StagingDir, dest_dir: str) -> str:
    """Atomically move a completed staging dir to ``dest_dir``, then clean up.

    Uses ``os.replace`` (atomic on the same filesystem — the working folder and
    its ``.tmp/`` always share one). The parent of ``dest_dir`` is created first.
    ``dest_dir`` must not already exist (name dedup happens upstream in naming).
    Returns ``dest_dir``.
    """
    if os.path.exists(dest_dir):
        raise FileExistsError(f"destination already exists: {dest_dir}")
    os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
    os.replace(staging.path, dest_dir)
    _prune_empty_tmp_parents(staging)
    return dest_dir


def _prune_empty_tmp_parents(staging: StagingDir) -> None:
    """Remove now-empty ``.tmp/{type}`` and ``.tmp`` dirs, best effort.

    Never touches non-empty directories (other concurrent jobs may still be
    staging there), so this is safe to call from any finishing job.
    """
    type_dir = os.path.join(staging.working_folder, TMP_DIRNAME, staging.media_type.tmp_name)
    tmp_root = os.path.join(staging.working_folder, TMP_DIRNAME)
    for d in (type_dir, tmp_root):
        try:
            os.rmdir(d)  # only succeeds when empty
        except OSError:
            break  # non-empty or missing — stop pruning upward


def discard(staging: StagingDir) -> None:
    """Delete a staging dir outright (e.g. user abandons a failed job)."""
    shutil.rmtree(staging.path, ignore_errors=True)
    _prune_empty_tmp_parents(staging)
