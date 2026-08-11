"""The Processing panel, drive release, and unattended naming.

A capture job outlives the media read that started it, so three things have to
hold: the drive is released as soon as the hardware is done, the job stays
visible while it finishes in the background, and nothing modal interrupts when
unattended naming is on.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from attic.controllers.base import JobRequest
from attic.controllers.floppy import FloppyCaptureWorker, parse_track_total
from attic.core.config import MediaType, Status
from attic.core.staging import StagingDir


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# --- progress denominators --------------------------------------------------


@pytest.mark.parametrize("line,expected", [
    ("Reading c=0-79:h=0-1 revs=2", 160),
    ("Reading c=0-39:h=0 revs=3", 40),
    ("Converting c=0-79:h=0-1 -> c=0-79:h=0-1", 160),
    ("Reading c=0-7,9-12:h=0-1 revs=2", 24),   # gw SET syntax: ranges + lists
    ("T0.0: IBM MFM (18/18 sectors)", None),
    ("", None),
])
def test_parse_track_total(line, expected):
    assert parse_track_total(line) == expected


# --- the panel --------------------------------------------------------------


def test_panel_tracks_a_job_from_capture_through_finalize(qapp):
    from attic.ui.processing_panel import ProcessingPanel

    panel = ProcessingPanel()
    panel.start_job("sess-1", MediaType.FLOPPY, "Backup 1994")
    assert panel.rows() == ["[FLOPPY] Backup 1994 - starting"]

    panel.set_stage("sess-1", "Reading floppy (flux)")
    panel.set_percent("sess-1", 50)
    assert panel.rows() == ["[FLOPPY] Backup 1994 - Reading floppy (flux)"]

    # The finalize pool only knows the resolved name, not the session id.
    panel.rename_job("sess-1", "Backup 1994")
    panel.set_stage_by_name("Backup 1994", "Compressing flux")
    assert panel.rows() == ["[FLOPPY] Backup 1994 - Compressing flux"]

    panel.finish_by_name("Backup 1994", "archived")
    assert panel.rows() == ["[FLOPPY] Backup 1994 - archived"]


def test_panel_mixes_media_types(qapp):
    from attic.ui.processing_panel import ProcessingPanel

    panel = ProcessingPanel()
    panel.start_job("a", MediaType.FLOPPY, "Disk A")
    panel.start_job("b", MediaType.HDD, "/dev/sdb")
    panel.start_job("c", MediaType.OPTICAL, "Mix CD")
    assert [r.split(" - ")[0] for r in panel.rows()] == [
        "[FLOPPY] Disk A", "[HDD] /dev/sdb", "[CD/DVD] Mix CD",
    ]


def test_panel_progress_is_determinate_only_when_measured(qapp):
    from attic.ui.processing_panel import ProcessingPanel

    panel = ProcessingPanel()
    panel.start_job("a", MediaType.FLOPPY, "Disk A")
    row = panel._rows["a"]
    # No measurement yet -> busy bar (Qt's convention for indeterminate).
    assert (row.bar.minimum(), row.bar.maximum()) == (0, 0)

    panel.set_percent("a", 42)
    assert (row.bar.maximum(), row.bar.value()) == (100, 42)

    # A new stage drops back to busy rather than keeping a stale percentage.
    panel.set_stage("a", "Extracting files")
    assert (row.bar.minimum(), row.bar.maximum()) == (0, 0)


def test_unknown_job_ids_are_ignored(qapp):
    from attic.ui.processing_panel import ProcessingPanel

    panel = ProcessingPanel()
    panel.set_stage("nope", "x")
    panel.set_percent("nope", 5)
    panel.rename_job("nope", "y")
    panel.finish_by_name("nope", "z")
    assert panel.rows() == []


# --- double-click job action popup -------------------------------------------


def _stub_exec(action: str | None):
    """Stand in for JobActionDialog.exec(): simulate one button click (or a
    dismiss, for None) instead of blocking on a real modal event loop."""
    def _exec(self):
        if action == "skip":
            self._choose_skip()
        elif action == "cancel":
            self._choose_cancel()
        return 0
    return _exec


def test_double_click_during_capture_offers_skip_and_cancel_when_supported(qapp, monkeypatch):
    from attic.ui.processing_panel import JobActionDialog, ProcessingPanel

    panel = ProcessingPanel()
    panel.start_job("a", MediaType.OPTICAL, "Mix CD", supports_capture_control=True)

    emitted = []
    panel.capture_skip_requested.connect(emitted.append)
    panel.capture_cancel_requested.connect(emitted.append)

    monkeypatch.setattr(JobActionDialog, "exec", _stub_exec("skip"))
    panel._open_job_dialog(panel._items["a"])

    assert emitted == ["a"]


def test_double_click_during_capture_has_no_actions_when_unsupported(qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from attic.ui.processing_panel import ProcessingPanel

    panel = ProcessingPanel()
    panel.start_job("a", MediaType.FLOPPY, "Disk A")  # supports_capture_control=False

    shown = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda *a, **k: shown.append(a) or QMessageBox.StandardButton.Ok,
    )
    emitted = []
    panel.capture_skip_requested.connect(emitted.append)
    panel.capture_cancel_requested.connect(emitted.append)

    panel._open_job_dialog(panel._items["a"])

    assert emitted == []
    assert len(shown) == 1


def test_double_click_while_finalizing_emits_compress_actions(qapp, monkeypatch):
    from attic.ui.processing_panel import JobActionDialog, ProcessingPanel

    panel = ProcessingPanel()
    panel.start_job("a", MediaType.HDD, "/dev/sdb")
    panel.rename_job("a", "drive_004")
    assert panel._rows["a"].stage_kind == "finalizing"

    emitted = []
    panel.compress_cancel_requested.connect(emitted.append)

    monkeypatch.setattr(JobActionDialog, "exec", _stub_exec("cancel"))
    panel._open_job_dialog(panel._items["a"])

    assert emitted == ["drive_004"]


def test_dismissing_the_dialog_emits_nothing(qapp, monkeypatch):
    from attic.ui.processing_panel import JobActionDialog, ProcessingPanel

    panel = ProcessingPanel()
    panel.start_job("a", MediaType.OPTICAL, "Mix CD", supports_capture_control=True)

    emitted = []
    panel.capture_skip_requested.connect(emitted.append)
    panel.capture_cancel_requested.connect(emitted.append)

    monkeypatch.setattr(JobActionDialog, "exec", _stub_exec(None))  # "Dismiss"
    panel._open_job_dialog(panel._items["a"])

    assert emitted == []


# --- drive release ----------------------------------------------------------


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(line + "\n" for line in lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode

    def terminate(self):
        pass


def test_drive_is_released_before_decode_not_after(qapp, tmp_path, monkeypatch):
    """The whole point: Begin Capture returns while the job is still working."""
    st = StagingDir(str(tmp_path), MediaType.FLOPPY, "s1")
    os.makedirs(st.path, exist_ok=True)
    flux, image = st.child("floppy.scp"), st.child("floppy.img")

    events: list[str] = []
    calls = iter([
        (["T0.0: IBM MFM (18/18 sectors)"], lambda: open(flux, "wb").write(b"\0" * 16)),
        (["Converting c=0-0:h=0"], lambda: (events.append("decode"),
                                            open(image, "wb").write(b"\0" * 16))),
    ])

    def fake_popen(cmd, **_kw):
        lines, effect = next(calls)
        effect()
        return _FakeProc(lines)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        "attic.controllers.floppy.fsdetect.detect_filesystem",
        lambda *a, **k: type("D", (), dict(
            fstype="unrecognized_filesystem", label="", recognized=False, method="n"
        ))(),
    )

    worker = FloppyCaptureWorker(
        JobRequest(staging_root="/unused", media_type=MediaType.FLOPPY),
        capture_flux=True,
    )
    worker.drive_released.connect(lambda: events.append("released"))
    worker.capture(st)

    # Released first, decode second -- not the other way round.
    assert events == ["released", "decode"]


def test_drive_release_is_emitted_once(qapp, tmp_path, monkeypatch):
    st = StagingDir(str(tmp_path), MediaType.FLOPPY, "s2")
    os.makedirs(st.path, exist_ok=True)
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, **kw: _FakeProc(["Command Failed: GetFluxStatus: No Index"]),
    )
    worker = FloppyCaptureWorker(
        JobRequest(staging_root="/unused", media_type=MediaType.FLOPPY),
        capture_flux=True,
    )
    count = []
    worker.drive_released.connect(lambda: count.append(1))
    art = worker.capture(st)
    worker.release_drive()  # the run() backstop would fire this too
    assert art.status == Status.FAILED
    assert len(count) == 1
