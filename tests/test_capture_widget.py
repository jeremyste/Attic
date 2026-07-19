"""Regression: photo dialog buttons must not steal SPACE/Enter shortcuts."""

import unittest.mock as mock

import numpy as np
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent

import attic.ui.webcam.capture_widget as cw


class _FakeCap:
    """Simulates a webcam whose maximum mode is 1080p; clamps requests to it."""

    MAX_W, MAX_H = 1920, 1080

    def __init__(self, *a):
        self._w, self._h = 640, 480

    def isOpened(self):
        return True

    def set(self, prop, value):
        import cv2

        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            self._w = min(int(value), self.MAX_W)
        elif prop == cv2.CAP_PROP_FRAME_HEIGHT:
            self._h = min(int(value), self.MAX_H)
        return True

    def read(self):
        return True, np.zeros((self._h, self._w, 3), dtype=np.uint8)

    def release(self):
        pass


def _dialog():
    with mock.patch.object(cw.cv2, "VideoCapture", _FakeCap):
        return cw.WebcamCaptureDialog("/tmp/attic-test.jpg", shape=cw.SHAPE_RECT)


def test_buttons_are_not_default_and_take_no_focus(qapp):
    dlg = _dialog()
    try:
        for btn in (dlg.accept_btn, dlg.retry_btn, dlg.skip_btn):
            assert btn.autoDefault() is False
            assert btn.isDefault() is False
            assert btn.focusPolicy() == Qt.FocusPolicy.NoFocus
    finally:
        dlg._teardown()


def test_enter_in_live_mode_does_not_skip_or_accept(qapp):
    dlg = _dialog()
    try:
        dlg._reviewing = False
        dlg.accept_btn.setEnabled(False)
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        dlg.keyPressEvent(ev)
        assert ev.isAccepted()          # consumed, not passed to default button
        assert dlg.result_path == ""    # nothing saved, dialog not skipped
    finally:
        dlg._teardown()


def test_space_is_consumed(qapp):
    dlg = _dialog()
    try:
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
        dlg.keyPressEvent(ev)
        assert ev.isAccepted()
    finally:
        dlg._teardown()


def test_configure_max_resolution_picks_highest_supported(qapp):
    # The fake camera maxes at 1080p; the probe must select that, not the
    # default VGA nor an unreachable 4K.
    cap = _FakeCap()
    chosen = cw.configure_max_resolution(cap)
    assert chosen == (1920, 1080)


def test_dialog_configures_resolution_on_open(qapp):
    dlg = _dialog()
    try:
        assert dlg._resolution == (1920, 1080)
    finally:
        dlg._teardown()
