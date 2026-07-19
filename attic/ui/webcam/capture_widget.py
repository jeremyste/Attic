"""Webcam photo-capture dialog with live boundary overlay.

Flow (Task.md): live preview with the detected boundary drawn in real time ->
SPACE freezes the frame and applies the detected crop, showing the cropped result
for review -> accept saves ``{name}_photo.jpg`` / 'R' returns to live. If auto
detection clearly fails, the review step falls back to a manual click-and-drag
rectangle crop rather than presenting a bad auto-crop.

Runs against a real camera via OpenCV; it degrades gracefully (a Skip path) when
no camera is available so capture is never blocked.
"""

from __future__ import annotations

import cv2
import numpy as np
from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QImage, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import detect

SHAPE_RECT = "rect"
SHAPE_CIRCLE = "circle"


class _ImageView(QLabel):
    """A QLabel that reports click-drag rectangles for manual cropping."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 480)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drag_start: QPoint | None = None
        self._drag_rect: QRect | None = None
        self.manual_mode = False
        self.on_manual_rect = None  # callback(QRect in label coords)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if self.manual_mode and ev.button() == Qt.MouseButton.LeftButton:
            self._drag_start = ev.pos()
            self._drag_rect = QRect(ev.pos(), ev.pos())
            self.update()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if self.manual_mode and self._drag_start is not None:
            self._drag_rect = QRect(self._drag_start, ev.pos()).normalized()
            self.update()

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        if self.manual_mode and self._drag_start is not None:
            self._drag_rect = QRect(self._drag_start, ev.pos()).normalized()
            self._drag_start = None
            if self.on_manual_rect and self._drag_rect.width() > 5:
                self.on_manual_rect(self._drag_rect)

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if self.manual_mode and self._drag_rect is not None:
            painter = QPainter(self)
            painter.setPen(QPen(Qt.GlobalColor.cyan, 2, Qt.PenStyle.DashLine))
            painter.drawRect(self._drag_rect)


class WebcamCaptureDialog(QDialog):
    """Modal dialog that captures and crops a photo of the media item.

    On accept, ``result_path`` holds the saved JPG path (or "" if skipped).
    """

    def __init__(self, save_path: str, shape: str = SHAPE_RECT,
                 camera_index: int = 0, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Capture item photo")
        self.save_path = save_path
        self.shape = shape
        self.result_path = ""

        self._frame: np.ndarray | None = None  # latest live frame (BGR)
        self._frozen: np.ndarray | None = None  # frozen full frame
        self._cropped: np.ndarray | None = None  # crop being reviewed
        self._reviewing = False

        self.view = _ImageView()
        self.view.on_manual_rect = self._apply_manual_rect

        self.hint = QLabel("SPACE = freeze & crop   ·   R = retry   ·   Enter = accept")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.accept_btn = QPushButton("Accept")
        self.retry_btn = QPushButton("Retry (R)")
        self.skip_btn = QPushButton("Skip photo")
        self.accept_btn.clicked.connect(self._accept_crop)
        self.retry_btn.clicked.connect(self._back_to_live)
        self.skip_btn.clicked.connect(self._skip)
        self.accept_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)

        # The SPACE (freeze) / Enter (accept) / R (retry) shortcuts are handled by
        # the dialog's keyPressEvent. Stop the buttons from stealing those keys:
        # no button is a default (Enter) and none takes keyboard focus (Space),
        # so they respond only to mouse clicks.
        for btn in (self.retry_btn, self.skip_btn, self.accept_btn):
            btn.setAutoDefault(False)
            btn.setDefault(False)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        buttons = QHBoxLayout()
        buttons.addWidget(self.retry_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.skip_btn)
        buttons.addWidget(self.accept_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.hint)
        layout.addLayout(buttons)

        # Ensure the dialog itself receives key events for the shortcuts.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._cap = cv2.VideoCapture(camera_index)
        if not self._cap or not self._cap.isOpened():
            QTimer.singleShot(0, self._no_camera)
            return

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)  # ~30 fps

    # --- camera loop --------------------------------------------------------

    def _tick(self) -> None:
        if self._reviewing:
            return
        ok, frame = self._cap.read()
        if not ok:
            return
        self._frame = frame
        overlay = frame.copy()
        self._draw_overlay(overlay)
        self._show(overlay)

    def _draw_overlay(self, img: np.ndarray) -> None:
        if self.shape == SHAPE_CIRCLE:
            circle = detect.detect_circle(img)
            if circle:
                cv2.circle(img, (circle.x, circle.y), circle.r, (0, 255, 0), 3)
        else:
            quad = detect.detect_rectangle(img)
            if quad is not None:
                cv2.polylines(img, [quad.astype(int)], True, (0, 255, 0), 3)

    # --- freeze / crop ------------------------------------------------------

    def _freeze_and_crop(self) -> None:
        if self._frame is None:
            return
        self._frozen = self._frame.copy()
        self._reviewing = True
        crop = self._auto_crop(self._frozen)
        if crop is not None and crop.size > 0:
            self._set_cropped(crop)
            self.hint.setText("Review crop · Accept, or Retry (R) for live")
        else:
            # Auto-detection failed — offer manual drag crop on the frozen frame.
            self._show(self._frozen)
            self.view.manual_mode = True
            self.accept_btn.setEnabled(False)
            self.retry_btn.setEnabled(True)
            self.hint.setText("Auto-detect failed · drag a rectangle to crop manually")

    def _auto_crop(self, frame: np.ndarray) -> np.ndarray | None:
        if self.shape == SHAPE_CIRCLE:
            circle = detect.detect_circle(frame)
            return detect.crop_circle(frame, circle) if circle else None
        quad = detect.detect_rectangle(frame)
        return detect.four_point_transform(frame, quad) if quad is not None else None

    def _apply_manual_rect(self, rect: QRect) -> None:
        if self._frozen is None:
            return
        # Map label-space rect to image-space using the displayed pixmap geometry.
        crop = _map_and_crop(self._frozen, self.view, rect)
        if crop is not None and crop.size > 0:
            self.view.manual_mode = False
            self._set_cropped(crop)
            self.hint.setText("Review crop · Accept, or Retry (R)")

    def _set_cropped(self, crop: np.ndarray) -> None:
        self._cropped = crop
        self._show(crop)
        self.accept_btn.setEnabled(True)
        self.retry_btn.setEnabled(True)

    def _back_to_live(self) -> None:
        self._reviewing = False
        self._frozen = None
        self._cropped = None
        self.view.manual_mode = False
        self.accept_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self.hint.setText("SPACE = freeze & crop   ·   R = retry   ·   Enter = accept")

    # --- finish -------------------------------------------------------------

    def _accept_crop(self) -> None:
        if self._cropped is None:
            return
        cv2.imwrite(self.save_path, self._cropped, [cv2.IMWRITE_JPEG_QUALITY, 92])
        self.result_path = self.save_path
        self._teardown()
        self.accept()

    def _skip(self) -> None:
        self.result_path = ""
        self._teardown()
        self.reject()

    def _no_camera(self) -> None:
        QMessageBox.information(
            self, "No camera",
            "No webcam was detected. Continuing without a photo.",
        )
        self._skip()

    # --- helpers ------------------------------------------------------------

    def _show(self, bgr: np.ndarray) -> None:
        self.view.setPixmap(
            _bgr_to_pixmap(bgr).scaled(
                self.view.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def keyPressEvent(self, ev) -> None:
        key = ev.key()
        if key == Qt.Key.Key_Space:
            # Freeze in live mode; ignore (don't let it activate anything) in review.
            if not self._reviewing:
                self._freeze_and_crop()
            ev.accept()
        elif key == Qt.Key.Key_R:
            self._back_to_live()
            ev.accept()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Enter accepts only when a crop is ready; never falls through to the
            # dialog's default button (which would otherwise skip/close).
            if self.accept_btn.isEnabled():
                self._accept_crop()
            ev.accept()
        else:
            super().keyPressEvent(ev)

    def _teardown(self) -> None:
        if hasattr(self, "_timer"):
            self._timer.stop()
        if self._cap:
            self._cap.release()

    def closeEvent(self, ev) -> None:
        self._teardown()
        super().closeEvent(ev)


def _bgr_to_pixmap(bgr: np.ndarray) -> QPixmap:
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(image.copy())


def _map_and_crop(frame: np.ndarray, view: _ImageView, rect: QRect) -> np.ndarray | None:
    """Translate a label-space selection rectangle into frame pixels and crop."""
    pm = view.pixmap()
    if pm is None or pm.isNull():
        return None
    # The scaled pixmap is centered in the label; compute its offset + scale.
    lw, lh = view.width(), view.height()
    pw, ph = pm.width(), pm.height()
    off_x = (lw - pw) / 2
    off_y = (lh - ph) / 2
    fh, fw = frame.shape[:2]
    sx = fw / pw
    sy = fh / ph
    x0 = int(max((rect.left() - off_x) * sx, 0))
    y0 = int(max((rect.top() - off_y) * sy, 0))
    x1 = int(min((rect.right() - off_x) * sx, fw))
    y1 = int(min((rect.bottom() - off_y) * sy, fh))
    if x1 <= x0 or y1 <= y0:
        return None
    return frame[y0:y1, x0:x1]
