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

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Switching to this tab re-shows the log view; Qt doesn't preserve a
        # "was scrolled to bottom" intent across that, so it can land on
        # whatever scroll position the widget happened to have internally
        # rather than the most recent log lines. Force it back to the bottom
        # every time the tab becomes visible.
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def set_stage(self, text: str) -> None:
        self.stage_label.setText(text)

    def set_busy(self, busy: bool) -> None:
        self.begin_btn.setEnabled(not busy)

    def prompt_physical_label(self) -> LabelOutcome | None:
        """Show the pre-capture label+photo dialog; None if cancelled."""
        settings = self.context.settings
        dlg = PhysicalLabelDialog(
            self.media_type, parent=self,
            camera_index=settings.camera_index, skip_photo=settings.skip_photo,
        )
        if not dlg.exec():
            return None
        return dlg.outcome()

    def confirm_media_loaded(self, what: str) -> bool:
        """Gate the read on the user physically loading the media (their
        requested workflow: photo -> label -> insert -> backup)."""
        from PyQt6.QtWidgets import QMessageBox

        reply = QMessageBox.information(
            self, "Load media",
            f"Insert/load the {what} now, then click OK to begin capture.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        return reply == QMessageBox.StandardButton.Ok
