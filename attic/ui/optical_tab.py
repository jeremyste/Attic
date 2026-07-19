"""Optical pipeline tab: physical-label prompt, ddrescue imaging, rescue bar."""

from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit

from ..controllers.base import JobRequest
from ..controllers.optical import OpticalCaptureWorker
from ..core.config import MediaType
from .app_context import AppContext
from .base_tab import PipelineTab
from .widgets.rescue_bar import RescueBar


class OpticalTab(PipelineTab):
    def __init__(self, context: AppContext, parent=None):
        super().__init__(context, MediaType.OPTICAL, parent)
        self._worker: OpticalCaptureWorker | None = None

        self.device_edit = QLineEdit(context.settings.optical_device)
        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Optical device:"))
        dev_row.addWidget(self.device_edit, 1)
        self._layout.addLayout(dev_row)

        self.bar = RescueBar()
        self._layout.addWidget(QLabel("Rescue progress:"))
        self._layout.addWidget(self.bar)
        self._install_common()

        self.begin_btn.clicked.connect(self._begin)

    def apply_settings(self) -> None:
        self.device_edit.setText(self.context.settings.optical_device)

    def _begin(self) -> None:
        # Workflow: photo + label first, then load the disc, then read.
        outcome = self.prompt_physical_label()
        if outcome is None:
            return
        if not self.confirm_media_loaded("disc"):
            return
        self.bar.clear()
        self.set_busy(True)
        self.set_stage("Starting…")

        request = JobRequest(
            working_folder=self.context.session.working_folder,
            media_type=MediaType.OPTICAL,
            physical_label=outcome.physical_label,
            source_id=self.device_edit.text().strip() or "/dev/sr0",
            photo_path=outcome.photo_path,
        )
        worker = OpticalCaptureWorker(request, retries=self.context.settings.ddrescue_retries)
        worker.stage.connect(self.set_stage)
        worker.log.connect(self.append_log)
        worker.map_progress.connect(self.bar.set_summary)
        worker.captured.connect(self._on_captured)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda: self.set_busy(False))
        self._worker = worker
        worker.start()

    def _on_captured(self, request, staging, artifacts) -> None:
        self.set_stage("Naming / finalizing")
        self.context.route_single(request, staging, artifacts)
        self.set_stage("Idle — ready for next disc")

    def _on_failed(self, summary: str) -> None:
        self.append_log(f"FAILED: {summary}")
        self.set_stage("Failed — see log")
