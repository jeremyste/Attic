"""Optical capture worker: auto-eject behavior.

Only the eject wiring is covered here -- the rest of ``capture()`` (imaging,
detection, extraction, DVD-video conversion) is exercised by their own
dedicated modules' tests.
"""

from __future__ import annotations

import os

import pytest

import attic.controllers.optical as optical_mod
from attic.controllers.base import JobRequest
from attic.controllers.ddrescue_runner import DdrescueOutcome
from attic.controllers.optical import OpticalCaptureWorker
from attic.core.config import MediaType
from attic.core.fsdetect import FsDetection
from attic.core.staging import StagingDir


@pytest.fixture
def staging(tmp_path):
    st = StagingDir(str(tmp_path), MediaType.OPTICAL, "sess1")
    os.makedirs(st.path, exist_ok=True)
    return st


def _worker(monkeypatch, *, eject_on_complete=True, bad_bytes=0):
    """A worker whose ddrescue/detection steps are stubbed to a clean,
    unrecognized-filesystem result -- capture() only needs to reach the
    eject call, not do any real imaging/extraction."""
    monkeypatch.setattr(
        optical_mod, "run_ddrescue",
        lambda *a, **k: DdrescueOutcome(
            returncode=0, stderr_tail="",
            last_summary=type("S", (), {"bad_bytes": bad_bytes, "rescued_bytes": 1})(),
        ),
    )
    monkeypatch.setattr(
        optical_mod.fsdetect, "detect_filesystem",
        lambda *a, **k: FsDetection(fstype="", label="", recognized=False, method="test"),
    )
    request = JobRequest(working_folder="/unused", media_type=MediaType.OPTICAL)
    return OpticalCaptureWorker(request, eject_on_complete=eject_on_complete)


def test_ejects_the_device_when_enabled(monkeypatch, fake_run, staging):
    fake_run.when("eject", returncode=0)
    worker = _worker(monkeypatch, eject_on_complete=True)

    worker.capture(staging)

    argv = fake_run.find("eject")
    assert argv is not None
    assert argv == ["eject", "/dev/sr0"]


def test_does_not_eject_when_disabled(monkeypatch, fake_run, staging):
    worker = _worker(monkeypatch, eject_on_complete=False)

    worker.capture(staging)

    assert not fake_run.ran("eject")


def test_eject_failure_is_logged_not_fatal(monkeypatch, fake_run, staging):
    fake_run.when("eject", returncode=1, stderr="eject: unable to eject")
    worker = _worker(monkeypatch, eject_on_complete=True)
    logs = []
    worker.log.connect(logs.append)

    artifacts = worker.capture(staging)  # must not raise

    assert any("eject" in line.lower() for line in logs)
    assert artifacts is not None


def test_ejects_the_requested_device(monkeypatch, fake_run, staging):
    fake_run.when("eject", returncode=0)
    monkeypatch.setattr(
        optical_mod, "run_ddrescue",
        lambda *a, **k: DdrescueOutcome(
            returncode=0, stderr_tail="",
            last_summary=type("S", (), {"bad_bytes": 0, "rescued_bytes": 1})(),
        ),
    )
    monkeypatch.setattr(
        optical_mod.fsdetect, "detect_filesystem",
        lambda *a, **k: FsDetection(fstype="", label="", recognized=False, method="test"),
    )
    request = JobRequest(
        working_folder="/unused", media_type=MediaType.OPTICAL, source_id="/dev/sr1",
    )
    worker = OpticalCaptureWorker(request, eject_on_complete=True)

    worker.capture(staging)

    assert fake_run.find("eject") == ["eject", "/dev/sr1"]
