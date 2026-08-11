"""CaptureWorker's base run()/skip/abort semantics.

Shared by every CaptureWorker subclass (floppy, optical -- HDD's rescue phase
has its own worker with equivalent request_skip/request_abort, tested
alongside the rest of controllers/hdd.py). request_skip() just asks the
current step to wrap up early and keep whatever it produced; request_abort()
additionally tells run() to discard the result and emit `aborted` instead of
`captured`.
"""

from __future__ import annotations

import pytest

from attic.controllers.base import CaptureArtifacts, CaptureWorker, JobRequest
from attic.core.config import MediaType, Status


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _StubCaptureWorker(CaptureWorker):
    def capture(self, staging):
        return CaptureArtifacts(raw_image_path="", log_path="", status=Status.OK)


def _worker(tmp_path):
    return _StubCaptureWorker(
        JobRequest(staging_root=str(tmp_path), media_type=MediaType.OPTICAL)
    )


def test_run_emits_captured_normally(qapp, tmp_path):
    worker = _worker(tmp_path)
    captured, aborted = [], []
    worker.captured.connect(lambda *a: captured.append(a))
    worker.aborted.connect(lambda *a: aborted.append(a))

    worker.run()

    assert len(captured) == 1
    assert aborted == []


def test_request_abort_discards_instead_of_emitting_captured(qapp, tmp_path):
    worker = _worker(tmp_path)
    worker.request_abort()
    captured, aborted = [], []
    worker.captured.connect(lambda *a: captured.append(a))
    worker.aborted.connect(lambda *a: aborted.append(a))

    worker.run()

    assert captured == []
    assert len(aborted) == 1
    request, staging = aborted[0]
    assert request is worker.request
    assert staging is worker.staging


def test_request_skip_does_not_set_abort(qapp, tmp_path):
    # isInterruptionRequested() is itself gated on the QThread having actually
    # been started (Qt only honors it once QThread::run() is executing), so a
    # thread that's never been .start()-ed can't observe it here -- what this
    # can verify directly is that request_skip() leaves _abort_requested
    # (our own flag, checked by run() after capture() returns) alone.
    worker = _worker(tmp_path)

    worker.request_skip()

    assert worker._abort_requested is False


def test_request_abort_sets_the_abort_flag(qapp, tmp_path):
    worker = _worker(tmp_path)

    worker.request_abort()

    assert worker._abort_requested is True
