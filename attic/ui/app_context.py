"""Shared orchestration wiring capture results into naming + finalize + catalog.

Centralizes the post-capture decision every pipeline shares:
  * A labeled volume (physical label entered or a filesystem label detected) gets
    the naming dialog for confirmation, then goes straight to the finalize pool.
  * An unlabeled volume is NOT blocked: it finalizes under its fallback name and
    is queued in the Pending Labels panel for the user to name whenever.
  * A capture that failed outright records a failed catalog row pointing at the
    surviving staging dir — nothing is silently lost.

Kept separate from the main window so the routing logic stays cohesive.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from PyQt6.QtWidgets import QWidget

from ..controllers.base import CaptureArtifacts, JobRequest, base_row
from ..controllers.compress_pool import FinalizePool, FinalizeRequest
from ..controllers.hdd import HddExtractResult
from ..core import catalog, naming
from ..core.catalog import CatalogRow
from ..core.config import MediaType, Status
from ..core.settings import AppSettings
from ..core.staging import StagingDir
from .naming_dialog import NamingDialog
from .pending_labels_panel import PendingItem, PendingLabelsPanel
from .processing_panel import ProcessingPanel
from .session import Session


@dataclass
class AppContext:
    session: Session
    finalize_pool: FinalizePool
    pending_panel: PendingLabelsPanel
    processing_panel: ProcessingPanel | None = None
    settings: AppSettings = field(default_factory=AppSettings)
    parent: QWidget | None = None
    # chosen_name -> PendingItem to add to the panel once finalize completes.
    _pending_after: dict[str, PendingItem] = field(default_factory=dict)

    @property
    def staging_root(self) -> str:
        """Local scratch location for new capture jobs (see AppSettings.staging_root)."""
        return self.settings.resolved_staging_root()

    # Per media type, whether a fully clean + fully extracted job should skip
    # archiving any image at all. Floppy is deliberately absent: its image (or
    # flux) is small and is the master worth always keeping, since re-imaging
    # fragile physical media may not be possible later -- the opposite
    # tradeoff from a large HDD/optical image that's genuinely redundant with
    # a clean extraction.
    def _finalize_request(self, **kwargs) -> FinalizeRequest:
        """Build a FinalizeRequest with compression options from settings."""
        media_type = kwargs.get("media_type")
        skip_when_clean = {
            MediaType.HDD: self.settings.hdd_auto_skip_image_when_clean,
            MediaType.OPTICAL: self.settings.optical_auto_skip_image_when_clean,
        }.get(media_type, False)
        return FinalizeRequest(
            keep_raw=self.settings.keep_raw_image,
            zstd_level=self.settings.zstd_level,
            zstd_long=self.settings.zstd_long,
            skip_image_when_clean=skip_when_clean,
            **kwargs,
        )

    # --- single-volume pipelines (floppy, optical) --------------------------

    def route_single(
        self, request: JobRequest, staging: StagingDir, artifacts: CaptureArtifacts
    ) -> None:
        if artifacts.status == Status.FAILED:
            self._record_capture_failure(request, staging, artifacts)
            return

        resolution = naming.resolve_name(
            self.session.working_folder,
            request.media_type,
            physical_label=request.physical_label,
            detected_label=artifacts.detected_label,
            fallback_date=artifacts.fallback_date,
        )

        chosen = resolution.chosen_name
        # Queue in Pending Labels only when the name is a generated fallback AND
        # the user hasn't opted to silently accept generated names.
        queue_pending = (
            resolution.used_fallback and not self.settings.auto_accept_fallback_names
        )

        # Auto-accept means unattended: nothing modal may interrupt, because the
        # user is expected to be loading the next disk while this one processes.
        if not resolution.used_fallback and not self.settings.auto_accept_fallback_names:
            # Confirm/adjust with the naming dialog.
            dlg = NamingDialog(
                request.media_type,
                physical_label=request.physical_label,
                detected_label=artifacts.detected_label,
                suggested_name=resolution.chosen_name,
                fallback_date=artifacts.fallback_date,
                date_suspect=artifacts.fallback_date_suspect,
                parent=self.parent,
            )
            if dlg.exec():
                outcome = dlg.outcome()
                chosen = self._dedupe(request.media_type, outcome.chosen_name)

        row = base_row(
            request,
            sequence_number=resolution.sequence_number,
            chosen_name=chosen,
            detected_label=artifacts.detected_label,
            partition_label="",
            folder_path="",
            fallback_date=artifacts.fallback_date if resolution.used_fallback else "",
            filesystem_detected=artifacts.filesystem_detected,
            status=artifacts.status,
        )

        if queue_pending:
            self._pending_after[chosen] = PendingItem(
                self.session.working_folder, request.media_type, chosen
            )

        if self.processing_panel is not None:
            self.processing_panel.rename_job(request.session_id, chosen)

        self.finalize_pool.submit(
            self._finalize_request(
                working_folder=self.session.working_folder,
                media_type=request.media_type,
                staging=staging,
                raw_image_path=artifacts.raw_image_path,
                flux_path=artifacts.flux_path,
                chosen_name=chosen,
                rows=[row],
                log_path=artifacts.log_path,
                photos=request.photos,
            )
        )

    # --- HDD (multi-partition) ---------------------------------------------

    def route_hdd(
        self, request: JobRequest, staging: StagingDir, result: HddExtractResult
    ) -> None:
        resolution = naming.resolve_name(
            self.session.working_folder,
            request.media_type,
            physical_label=request.physical_label,
            detected_label="",  # no single drive-level detected label
            fallback_date=result.fallback_date,
        )
        chosen = resolution.chosen_name
        queue_pending = (
            resolution.used_fallback and not self.settings.auto_accept_fallback_names
        )

        # Auto-accept means unattended: nothing modal may interrupt.
        if not resolution.used_fallback and not self.settings.auto_accept_fallback_names:
            dlg = NamingDialog(
                request.media_type,
                physical_label=request.physical_label,
                detected_label="",
                suggested_name=resolution.chosen_name,
                fallback_date=result.fallback_date,
                parent=self.parent,
            )
            if dlg.exec():
                chosen = self._dedupe(request.media_type, dlg.outcome().chosen_name)

        # Finish the per-partition rows with the shared drive-level identity.
        for row in result.rows:
            row.chosen_name = chosen
            row.sequence_number = str(resolution.sequence_number)
            if resolution.used_fallback:
                row.fallback_date_used = result.fallback_date
            if result.read_bad_bytes >= 0:
                row.read_bad_bytes = str(result.read_bad_bytes)

        if queue_pending:
            self._pending_after[chosen] = PendingItem(
                self.session.working_folder, request.media_type, chosen
            )

        if self.processing_panel is not None:
            self.processing_panel.rename_job(request.session_id, chosen)

        self.finalize_pool.submit(
            self._finalize_request(
                working_folder=self.session.working_folder,
                media_type=request.media_type,
                staging=staging,
                raw_image_path=result.raw_image_path,
                chosen_name=chosen,
                rows=result.rows,
                log_path=result.log_path,
                photos=request.photos,
            )
        )

    # --- finalize completion ------------------------------------------------

    def on_finalize_done(self, final_dir: str, rows) -> None:
        """Connected to the finalize pool; surfaces any queued pending item."""
        for row in rows:
            item = self._pending_after.pop(row.chosen_name, None)
            if item is not None:
                self.pending_panel.add_pending(item)

    # --- capture-phase cancellation ------------------------------------------

    def record_capture_cancelled(self, request: JobRequest, staging: StagingDir) -> None:
        """A capture-phase "Cancel entirely" was chosen (before the job ever
        reached the finalize pool): discard the staging dir and record a
        cancelled catalog row, matching how the finalize pool's own cancel
        handles a job that's already further along.
        """
        if self.processing_panel is not None:
            self.processing_panel.finish_job(request.session_id, "Cancelled")
        catalog.append_row(
            self.session.working_folder,
            CatalogRow(
                media_type=request.media_type.value,
                source_id=request.source_id,
                physical_label_entered=request.physical_label,
                status=Status.CANCELLED.value,
                notes=(
                    "Cancelled by user during capture -- raw image/flux "
                    "discarded, not archived."
                ),
            ),
        )
        shutil.rmtree(staging.path, ignore_errors=True)

    # --- helpers ------------------------------------------------------------

    def _dedupe(self, media_type: MediaType, name: str) -> str:
        taken = catalog.existing_chosen_names(self.session.working_folder, media_type.value)
        return naming.dedupe_name(name, taken)

    def _record_capture_failure(
        self, request: JobRequest, staging: StagingDir, artifacts: CaptureArtifacts
    ) -> None:
        # Absolute, not staging.rel_path(): staging now lives under its own
        # staging_root, which may not be anywhere near the working folder this
        # note gets written into.
        note = f"Temp dir left for inspection: {staging.path}"
        if self.processing_panel is not None:
            self.processing_panel.finish_job(
                request.session_id,
                f"FAILED - {artifacts.error_summary or 'capture failed'}",
            )
        catalog.append_row(
            self.session.working_folder,
            CatalogRow(
                media_type=request.media_type.value,
                source_id=request.source_id,
                physical_label_entered=request.physical_label,
                status=Status.FAILED.value,
                error_summary=artifacts.error_summary or "capture failed",
                notes=note,
            ),
        )
