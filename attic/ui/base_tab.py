"""Shared scaffolding for the pipeline tabs (log view, stage label, buttons)."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.config import MediaType
from .app_context import AppContext
from .label_dialog import LabelOutcome, PhysicalLabelDialog


class PipelineTab(QWidget):
    """Base tab: a Begin Capture button, a stage label, and a log view."""

    def __init__(self, context: AppContext, media_type: MediaType, parent=None):
        super().__init__(parent)
        self.context = context
        self.media_type = media_type

        self.begin_btn = QPushButton("Begin Capture")
        self.stage_label = QLabel("Idle")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)

        self._layout = QVBoxLayout(self)
        # Subclasses insert their widgets, then call _install_common().

    def _install_common(self) -> None:
        self._layout.addWidget(self.stage_label)
        self._layout.addWidget(self.begin_btn)
        self._layout.addWidget(self.log_view, 1)

    # --- shared helpers -----------------------------------------------------

    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def set_stage(self, text: str) -> None:
        self.stage_label.setText(text)

    def set_busy(self, busy: bool) -> None:
        self.begin_btn.setEnabled(not busy)

    def prompt_physical_label(self) -> LabelOutcome | None:
        """Show the pre-capture label+photo dialog; None if cancelled."""
        dlg = PhysicalLabelDialog(self.media_type, parent=self)
        if not dlg.exec():
            return None
        return dlg.outcome()
