"""Every tab's Begin Capture path, with dialogs and workers stubbed out.

Regression: ``ProcessingPanel.start_job`` gained a media_type argument, but the
floppy tab kept calling it with two. Nothing in the suite ever invoked a tab's
``_begin``, so a plain TypeError shipped and crashed on the first click.

These tests run each tab's begin path far enough to hit every call it makes into
the shared panels and to construct its worker, without touching hardware. They
are deliberately shallow -- the point is that the wiring is *callable* and
mutually consistent, which is exactly what unit tests of the pieces cannot show.
"""

from __future__ import annotations

import pytest

from attic.core.config import MediaType
from attic.ui.label_dialog import LabelOutcome


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def ctx(qapp, tmp_path):
    from attic.controllers.compress_pool import FinalizePool
    from attic.ui.app_context import AppContext
    from attic.ui.pending_labels_panel import PendingLabelsPanel
    from attic.ui.processing_panel import ProcessingPanel
    from attic.ui.session import Session

    session = Session(str(tmp_path))
    session.ensure_skeleton()
    return AppContext(
        session=session,
        finalize_pool=FinalizePool(),
        pending_panel=PendingLabelsPanel(),
        processing_panel=ProcessingPanel(),
    )


class _StubWorker:
    """Stands in for a capture worker: records start, never touches hardware."""

    started = []

    def __init__(self, *a, **k):
        self.args, self.kwargs = a, k
        for name in (
            "stage", "log", "track_read", "captured", "failed", "finished",
            "drive_released", "map_progress", "progress", "pass_done", "done",
            "aborted",
        ):
            setattr(self, name, _Signal())

    def start(self):
        _StubWorker.started.append(self)

    def isRunning(self):
        return False


class _Signal:
    def connect(self, *_a, **_k):
        pass


def _no_dialogs(monkeypatch, tab, label="test3"):
    monkeypatch.setattr(
        type(tab), "prompt_physical_label", lambda self: LabelOutcome(label, {})
    )
    monkeypatch.setattr(type(tab), "confirm_media_loaded", lambda self, what: True)


def test_floppy_begin_registers_the_job_and_starts_a_worker(ctx, monkeypatch):
    import attic.ui.floppy_tab as mod

    _StubWorker.started.clear()
    monkeypatch.setattr(mod, "FloppyCaptureWorker", _StubWorker)
    tab = mod.FloppyTab(ctx)
    _no_dialogs(monkeypatch, tab)

    tab._begin()

    assert len(_StubWorker.started) == 1
    assert ctx.processing_panel.rows() == ["[FLOPPY] test3 - starting"]
    assert not tab.begin_btn.isEnabled()  # busy until the drive is released


def test_optical_begin_registers_the_job_and_starts_a_worker(ctx, monkeypatch):
    import attic.ui.optical_tab as mod

    _StubWorker.started.clear()
    monkeypatch.setattr(mod, "OpticalCaptureWorker", _StubWorker)
    tab = mod.OpticalTab(ctx)
    _no_dialogs(monkeypatch, tab, label="Mix CD")

    tab._begin()

    assert len(_StubWorker.started) == 1
    assert ctx.processing_panel.rows() == ["[CD/DVD] Mix CD - starting"]


def test_hdd_begin_registers_the_job_and_starts_a_pass(ctx, monkeypatch):
    import attic.ui.hdd_tab as mod
    from attic.core import devices

    _StubWorker.started.clear()
    monkeypatch.setattr(mod, "HddRescueWorker", _StubWorker)
    tab = mod.HddTab(ctx)

    device = devices.BlockDevice(
        name="sdz", path="/dev/sdz", model="TestDrive", size="1G",
        transport="usb", removable=True, has_system_mount=False,
    )
    monkeypatch.setattr(
        type(tab), "_begin_photo_first",
        lambda self: (device, LabelOutcome("Old Laptop", {})),
    )
    monkeypatch.setattr(type(tab), "_confirm_device", lambda self, d: True)

    tab._begin()

    assert len(_StubWorker.started) == 1
    assert ctx.processing_panel.rows() == ["[HDD] Old Laptop - starting"]


def test_unlabeled_capture_still_names_the_row(ctx, monkeypatch):
    import attic.ui.floppy_tab as mod

    _StubWorker.started.clear()
    monkeypatch.setattr(mod, "FloppyCaptureWorker", _StubWorker)
    tab = mod.FloppyTab(ctx)
    _no_dialogs(monkeypatch, tab, label="")

    tab._begin()

    assert ctx.processing_panel.rows() == ["[FLOPPY] Floppy (unlabeled) - starting"]
