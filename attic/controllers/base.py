"""Shared capture-worker scaffolding for the three pipelines.

Each capture job runs in its own ``QThread`` (``CaptureWorker`` subclass) so a
multi-minute read never freezes the GUI. Workers emit signals back to the main
thread for progress/log/completion. The heavy compression+checksum pass is not
done inline — the worker captures the raw image, then the finalize step is handed
to the shared background pool (see ``compress_pool``) so a slow compression on one
job never blocks starting the next capture on any pipeline.

None of this reimplements tool logic; it orchestrates the ``attic.core`` helpers.
"""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass, field
from datetime import datetime

from PyQt6.QtCore import QThread, pyqtSignal

from ..core import catalog, staging
from ..core.catalog import CatalogRow
from ..core.config import (
    COMPRESSED_IMAGE_SUFFIX,
    EXTRACTED_DIRNAME,
    LOG_SUFFIX,
    MediaType,
    RAW_IMAGE_SUFFIX,
    Status,
)
from ..core.staging import StagingDir


@dataclass
class JobRequest:
    """Everything a capture job needs, gathered before the read starts."""

    working_folder: str
    media_type: MediaType
    physical_label: str = ""
    source_id: str = ""  # device path, or "floppy"/"disc"
    photo_path: str = ""  # webcam photo already captured to this temp path, if any
    session_id: str = field(default_factory=staging.new_session_id)


@dataclass
class CaptureArtifacts:
    """What a pipeline's capture step produced inside its staging dir."""

    raw_image_path: str
    log_path: str = ""
    # Pipeline-specific detection/naming inputs the finalize step will use.
    detected_label: str = ""
    fallback_date: str = ""
    filesystem_detected: str = ""
    status: Status = Status.OK
    error_summary: str = ""
    notes: str = ""


class CaptureWorker(QThread):
    """Base QThread for a single capture job.

    Subclasses implement :meth:`capture` to produce the raw image (and, for the
    filesystem-bearing pipelines, run detection + extraction into the staging
    dir). The base handles staging allocation, uniform error trapping, and signal
    emission. Compression/checksum/catalog/promote is triggered separately via
    :meth:`build_row_and_paths` once compression (run in the pool) completes.
    """

    # int 0..100 (or -1 for indeterminate)
    progress = pyqtSignal(int)
    # short human stage label, e.g. "Reading", "Detecting filesystem"
    stage = pyqtSignal(str)
    # appended to the on-screen + on-disk log
    log = pyqtSignal(str)
    # emitted once capture+extract succeed; payload is (JobRequest, StagingDir,
    # CaptureArtifacts) so the tab can kick off the compression pool step.
    captured = pyqtSignal(object, object, object)
    # emitted on any fatal error with a summary string; the staging dir is left
    # in place for inspection.
    failed = pyqtSignal(str)

    def __init__(self, request: JobRequest, parent=None):
        super().__init__(parent)
        self.request = request
        self.staging: StagingDir | None = None

    # --- subclass hook ------------------------------------------------------

    def capture(self, staging: StagingDir) -> CaptureArtifacts:  # pragma: no cover
        """Produce the raw image (+ detection/extraction) inside ``staging``.

        Must be overridden. May emit :attr:`progress`/:attr:`stage`/:attr:`log`.
        """
        raise NotImplementedError

    # --- thread body --------------------------------------------------------

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            self.staging = staging.create_staging(
                self.request.working_folder,
                self.request.media_type,
                self.request.session_id,
            )
            self.log.emit(f"Staging: {self.staging.path}")
            artifacts = self.capture(self.staging)
            self.captured.emit(self.request, self.staging, artifacts)
        except Exception as exc:  # noqa: BLE001 - surface everything, never crash GUI
            summary = f"{type(exc).__name__}: {exc}"
            self.log.emit(traceback.format_exc())
            self.failed.emit(summary)

    # --- naming helpers for staging artifacts -------------------------------

    def raw_name(self, chosen_name: str) -> str:
        return f"{chosen_name}{RAW_IMAGE_SUFFIX}"

    def compressed_name(self, chosen_name: str) -> str:
        return f"{chosen_name}{COMPRESSED_IMAGE_SUFFIX}"

    def log_name(self, chosen_name: str) -> str:
        return f"{chosen_name}{LOG_SUFFIX}"


def base_row(
    request: JobRequest,
    *,
    sequence_number: int,
    chosen_name: str,
    detected_label: str,
    partition_label: str,
    folder_path: str,
    fallback_date: str,
    filesystem_detected: str,
    status: Status,
    error_summary: str = "",
    notes: str = "",
) -> CatalogRow:
    """Assemble a catalog row with the fields common to every pipeline.

    Image filename/size/hash fields are filled in later by the compression step.
    """
    return CatalogRow(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        media_type=request.media_type.value,
        sequence_number=str(sequence_number),
        source_id=request.source_id,
        physical_label_entered=request.physical_label,
        detected_label=detected_label,
        chosen_name=chosen_name,
        partition_label=partition_label,
        folder_path=folder_path,
        fallback_date_used=fallback_date,
        filesystem_detected=filesystem_detected,
        status=status.value,
        error_summary=error_summary,
        notes=notes,
    )


def extracted_dir(base_dir: str) -> str:
    """Single-volume ``Extracted Files/`` path under a job's final dir."""
    return os.path.join(base_dir, EXTRACTED_DIRNAME)
