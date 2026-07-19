"""Pre-capture physical-label prompt + optional webcam photo.

Shown BEFORE the user inserts/loads media (a loaded drive's sticker can't be read
by software, or by the user once it's inside a drive). For HDD this applies to the
whole drive, not per partition. The webcam photo is captured here too (during the
physical-label step, before "Begin Capture").
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core.config import MediaType
from .webcam.capture_widget import SHAPE_CIRCLE, SHAPE_RECT, WebcamCaptureDialog


@dataclass
class LabelOutcome:
    physical_label: str
    photo_path: str  # temp path to a captured JPG, or "" if none


class PhysicalLabelDialog(QDialog):
    """Prompt for the physical label and optionally capture a photo."""

    def __init__(self, media_type: MediaType, parent=None, *,
                 camera_index: int = 0, skip_photo: bool = False):
        super().__init__(parent)
        self.media_type = media_type
        self.camera_index = camera_index
        self.skip_photo = skip_photo
        self.setWindowTitle("Physical label")
        self._photo_path = ""

        prompt = QLabel("Physical label (leave blank if none):")
        self.label_edit = QLineEdit()

        self.photo_btn = QPushButton("Take photo…")
        self.photo_btn.clicked.connect(self._take_photo)
        self.photo_status = QLabel("No photo")
        if skip_photo:
            self.photo_btn.setEnabled(False)
            self.photo_status.setText("Photos disabled in settings")

        photo_row = QHBoxLayout()
        photo_row.addWidget(self.photo_btn)
        photo_row.addWidget(self.photo_status, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Continue")

        layout = QVBoxLayout(self)
        layout.addWidget(prompt)
        layout.addWidget(self.label_edit)
        layout.addLayout(photo_row)
        layout.addWidget(buttons)

    def _take_photo(self) -> None:
        # Circular media (optical) -> circle detection; rectangular otherwise.
        shape = SHAPE_CIRCLE if self.media_type == MediaType.OPTICAL else SHAPE_RECT
        fd, tmp_path = tempfile.mkstemp(prefix="attic-photo-", suffix=".jpg")
        os.close(fd)
        dlg = WebcamCaptureDialog(
            tmp_path, shape=shape, camera_index=self.camera_index, parent=self
        )
        dlg.exec()
        if dlg.result_path:
            self._photo_path = dlg.result_path
            self.photo_status.setText(f"Photo captured: {os.path.basename(dlg.result_path)}")
        else:
            self.photo_status.setText("No photo")

    def outcome(self) -> LabelOutcome:
        return LabelOutcome(
            physical_label=self.label_edit.text().strip(),
            photo_path=self._photo_path,
        )
