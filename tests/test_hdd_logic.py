import pytest

import attic.controllers.hdd as hdd_mod
from attic.controllers.ddrescue_runner import DdrescueOutcome
from attic.controllers.hdd import HddRescueWorker, _worse, normalize_parted_fstype
from attic.core.config import Status


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_normalize_known_fstypes():
    assert normalize_parted_fstype("fat32") == "fat32"
    assert normalize_parted_fstype("NTFS") == "ntfs"
    assert normalize_parted_fstype("ext4") == "ext4"


def test_normalize_unknown_returns_blank():
    assert normalize_parted_fstype("linux-swap") == ""
    assert normalize_parted_fstype("") == ""
    assert normalize_parted_fstype(None) == ""


def test_worse_status_rollup():
    assert _worse(Status.OK, Status.PARTIAL) == Status.PARTIAL
    assert _worse(Status.PARTIAL, Status.OK) == Status.PARTIAL
    assert _worse(Status.OK, Status.FAILED) == Status.FAILED
    assert _worse(Status.UNRECOGNIZED_FS, Status.PARTIAL) == Status.UNRECOGNIZED_FS
    assert _worse(Status.FAILED, Status.UNRECOGNIZED_FS) == Status.FAILED


# --- give-up controls (request_skip / request_abort) ------------------------


def test_rescue_worker_skip_does_not_set_abort(qapp):
    # isInterruptionRequested() is gated on the QThread actually being
    # started, so it can't be observed on a never-.start()-ed thread; what
    # this can verify directly is that request_skip() leaves _abort_requested
    # (our own flag, checked by run() after the pass completes) alone.
    worker = HddRescueWorker("/dev/sdz", "img", "map", "err", first_pass=True)

    worker.request_skip()

    assert worker._abort_requested is False


def test_rescue_worker_abort_emits_aborted_not_pass_done(qapp, monkeypatch):
    monkeypatch.setattr(
        hdd_mod, "run_ddrescue",
        lambda *a, **k: DdrescueOutcome(
            returncode=0, stderr_tail="",
            last_summary=type("S", (), {"bad_bytes": 0, "rescued_bytes": 100})(),
        ),
    )
    worker = HddRescueWorker("/dev/sdz", "img", "map", "err", first_pass=True)
    worker.request_abort()

    aborted, pass_done = [], []
    worker.aborted.connect(lambda: aborted.append(1))
    worker.pass_done.connect(lambda s: pass_done.append(s))

    worker.run()

    assert aborted == [1]
    assert pass_done == []


def test_rescue_worker_passes_stop_after_to_argv(qapp, monkeypatch):
    seen_argv = {}

    def _fake_run_ddrescue(argv, *a, **k):
        seen_argv["argv"] = argv
        return DdrescueOutcome(
            returncode=0, stderr_tail="",
            last_summary=type("S", (), {"bad_bytes": 0, "rescued_bytes": 100})(),
        )

    monkeypatch.setattr(hdd_mod, "run_ddrescue", _fake_run_ddrescue)
    worker = HddRescueWorker(
        "/dev/sdz", "img", "map", "err", first_pass=False, stop_after="scraping",
    )

    worker.run()

    assert not any(a.startswith("-r") for a in seen_argv["argv"])


def test_rescue_worker_normal_completion_emits_pass_done_not_aborted(qapp, monkeypatch):
    monkeypatch.setattr(
        hdd_mod, "run_ddrescue",
        lambda *a, **k: DdrescueOutcome(
            returncode=0, stderr_tail="",
            last_summary=type("S", (), {"bad_bytes": 0, "rescued_bytes": 100})(),
        ),
    )
    worker = HddRescueWorker("/dev/sdz", "img", "map", "err", first_pass=True)

    aborted, pass_done = [], []
    worker.aborted.connect(lambda: aborted.append(1))
    worker.pass_done.connect(lambda s: pass_done.append(s))

    worker.run()

    assert aborted == []
    assert len(pass_done) == 1
