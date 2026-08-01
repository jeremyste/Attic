"""Floppy pipeline tab: physical-label prompt, gw read, live cyl×head grid."""

from __future__ import annotations

from PyQt6.QtWidgets import QLabel

from ..controllers.base import JobRequest
from ..controllers.floppy import FloppyCaptureWorker
from ..core.config import MediaType
from .app_context import AppContext
from .base_tab import PipelineTab
from .widgets.track_grid import TrackGrid


class FloppyTab(PipelineTab):
    def __init__(self, context: AppContext, parent=None):
        super().__init__(context, MediaType.FLOPPY, parent)
        self._worker: FloppyCaptureWorker | None = None

        s = context.settings
        self.grid = TrackGrid(cylinders=s.floppy_cylinders, heads=s.floppy_heads)
        self._layout.addWidget(QLabel("Track read map (cylinder × head):"))
        self._layout.addWidget(self.grid)
        self._install_common()

        self.begin_btn.clicked.connect(self._begin)

    def apply_settings(self) -> None:
        s = self.context.settings
        self.grid.reset(cylinders=s.floppy_cylinders, heads=s.floppy_heads)

    def _begin(self) -> None:
        # Workflow: photo + label first, then insert the disk, then read.
        outcome = self.prompt_physical_label()
        if outcome is None:
            return
        if not self.confirm_media_loaded("floppy disk"):
            return
        s = self.context.settings
        self.grid.reset(cylinders=s.floppy_cylinders, heads=s.floppy_heads)
        self.set_busy(True)
        self.set_stage("Starting…")

        request = JobRequest(
            working_folder=self.context.session.working_folder,
            media_type=MediaType.FLOPPY,
            physical_label=outcome.physical_label,
            source_id="floppy",
            photos=outcome.photos,
        )
        worker = FloppyCaptureWorker(
            request,
            disk_format=s.floppy_format,
            device=s.floppy_device,
            capture_flux=s.floppy_capture_flux,
            flux_revs=s.floppy_flux_revs,
        )
        worker.stage.connect(self.set_stage)
        worker.log.connect(self.append_log)
        worker.track_read.connect(lambda tr: self.grid.set_track(tr.cyl, tr.head, tr.status))
        worker.captured.connect(self._on_captured)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda: self.set_busy(False))
        self._worker = worker
        worker.start()

    def _on_captured(self, request, staging, artifacts) -> None:
        self.set_stage("Naming / finalizing")
        self.context.route_single(request, staging, artifacts)
        self.set_stage("Idle — ready for next disk")

    def _on_failed(self, summary: str) -> None:
        self.append_log(f"FAILED: {summary}")
        self.set_stage("Failed — see log")
