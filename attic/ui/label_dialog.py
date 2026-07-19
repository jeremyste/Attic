"""Pre-capture physical-label prompt + optional webcam photo(s).

Shown BEFORE the user inserts/loads media (a loaded drive's sticker can't be read
by software, or by the user once it's inside a drive). For HDD this applies to the
whole drive, not per partition.

Rectangular media (floppy, HDD) carry labels/part numbers on BOTH sides, so they
get a front and a back photo; optical discs get a single photo. Each photo is
captured with the webcam dialog during this step, before "Begin Capture".
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core.config import (
    MediaType,
    PHOTO_BACK_SUFFIX,
    PHOTO_FRONT_SUFFIX,
    PHOTO_SUFFIX,
)
from .webcam.capture_widget import SHAPE_CIRCLE, SHAPE_RECT, WebcamCaptureDialog


@dataclass
class LabelOutcome:
    physical_label: str
    # captured photos mapped {filename_suffix: temp_path}
    photos: dict[str, str] = field(default_factory=dict)


def _photo_slots(media_type: MediaType) -> list[tuple[str, str]]:
    """Return the (button caption, filename suffix) photo slots for a media type."""
    if media_type == MediaType.OPTICAL:
        return [("Take photo…", PHOTO_SUFFIX)]
    return [
        ("Take front photo…", PHOTO_FRONT_SUFFIX),
        ("Take back photo…", PHOTO_BACK_SUFFIX),
    ]


class PhysicalLabelDialog(QDialog):
    """Prompt for the physical label and optionally capture photo(s)."""

    def __init__(self, media_type: MediaType, parent=None, *,
                 camera_index: int = 0, skip_photo: bool = False):
        super().__init__(parent)
        self.media_type = media_type
        self.camera_index = camera_index
        self.skip_photo = skip_photo
        self.setWindowTitle("Physical label")
        self._photos: dict[str, str] = {}  # suffix -> temp path

        prompt = QLabel("Physical label (leave blank if none):")
        self.label_edit = QLineEdit()

        layout = QVBoxLayout(self)
        layout.addWidget(prompt)
        layout.addWidget(self.label_edit)

        # One row per photo slot (single for optical, front+back otherwise).
        self._shape = SHAPE_CIRCLE if media_type == MediaType.OPTICAL else SHAPE_RECT
        for caption, suffix in _photo_slots(media_type):
            btn = QPushButton(caption)
            status = QLabel("No photo")
            if skip_photo:
                btn.setEnabled(False)
                status.setText("Photos disabled in settings")
            else:
                btn.clicked.connect(
                    lambda _checked, s=suffix, st=status: self._take_photo(s, st)
                )
            row = QHBoxLayout()
            row.addWidget(btn)
            row.addWidget(status, 1)
            layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Continue")
        layout.addWidget(buttons)

    def _take_photo(self, suffix: str, status: QLabel) -> None:
        fd, tmp_path = tempfile.mkstemp(prefix="attic-photo-", suffix=".jpg")
        os.close(fd)
        dlg = WebcamCaptureDialog(
            tmp_path, shape=self._shape, camera_index=self.camera_index, parent=self
        )
        dlg.exec()
        if dlg.result_path:
            self._photos[suffix] = dlg.result_path
            status.setText(f"Captured: {os.path.basename(dlg.result_path)}")
        else:
            status.setText("No photo")

    def outcome(self) -> LabelOutcome:
        return LabelOutcome(
            physical_label=self.label_edit.text().strip(),
            photos=dict(self._photos),
        )
