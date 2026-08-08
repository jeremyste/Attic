"""Cancel / skip-compression behavior of the finalize pool.

Most cases invoke ``_FinalizeTask.run()`` directly (bypassing the real
QThreadPool) with a pre-armed ``_JobControl`` -- deterministic, and exactly
what happens once a job is actually running. A couple of pool-level tests
cover the "still queued" path, which needs a real (but thread-count-capped)
QThreadPool to exercise ``tryTake``.
"""

from __future__ import annotations

import os
import threading

import pytest
from PyQt6.QtCore import QRunnable

import attic.core.compression as compression_mod
from attic.controllers.compress_pool import (
    FinalizePool,
    FinalizeRequest,
    _FinalizeTask,
    _JobControl,
    _Signals,
)
from attic.core.catalog import CatalogRow, read_rows
from attic.core.config import MediaType, Status
from attic.core.staging import create_staging
from attic.core.subprocess_util import CmdResult


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _staged_request(tmp_path, *, with_image=True, status=Status.OK):
    working_folder = str(tmp_path)
    st = create_staging(working_folder, MediaType.HDD, "sess1")
    raw_image_path = ""
    if with_image:
        raw_image_path = st.child("raw.img")
        with open(raw_image_path, "wb") as fh:
            fh.write(b"x" * 1000)
    rows = [CatalogRow(media_type="hdd", chosen_name="drive_001", status=status.value)]
    req = FinalizeRequest(
        working_folder=working_folder, media_type=MediaType.HDD, staging=st,
        raw_image_path=raw_image_path, chosen_name="drive_001", rows=rows,
    )
    return req, st


def _fake_zstd_ok(monkeypatch, fake_run):
    def zstd(argv):
        out = argv[argv.index("-o") + 1]
        with open(out, "wb") as fh:
            fh.write(b"z" * 100)
        return CmdResult(argv=argv, returncode=0)

    fake_run.when(lambda a: a and a[0] == "zstd")
    fake_run.rules[-1].result = zstd
    fake_run.when("sha256sum", stdout="deadbeef  x\n")


# --- cancel --------------------------------------------------------------


def test_cancel_before_compression_starts_discards_everything(tmp_path, qapp):
    req, st = _staged_request(tmp_path)
    signals = _Signals()
    cancelled = []
    signals.cancelled.connect(cancelled.append)
    control = _JobControl()
    control.cancel_event.set()
    task = _FinalizeTask(req, signals, control)

    task.run()

    assert cancelled == ["drive_001"]
    assert not st.exists()
    rows = read_rows(str(tmp_path))
    assert len(rows) == 1
    assert rows[0]["status"] == Status.CANCELLED.value
    assert rows[0]["chosen_name"] == "drive_001"


def test_cancel_mid_compression_discards_everything(tmp_path, qapp, monkeypatch):
    req, st = _staged_request(tmp_path)
    signals = _Signals()
    cancelled = []
    signals.cancelled.connect(cancelled.append)
    control = _JobControl()
    task = _FinalizeTask(req, signals, control)

    def fake_compress(*a, should_cancel=None, **k):
        control.cancel_event.set()  # "the user clicked Cancel while this ran"
        assert should_cancel()
        raise compression_mod.CompressionCancelled()

    monkeypatch.setattr(compression_mod, "compress_and_checksum", fake_compress)

    task.run()

    assert cancelled == ["drive_001"]
    assert not st.exists()


def test_pool_cancel_of_unknown_job_returns_false(qapp):
    pool = FinalizePool()
    assert pool.cancel("nope") is False
    assert pool.skip_compression("nope") is False


def test_pool_cancel_removes_a_still_queued_job(tmp_path, qapp):
    pool = FinalizePool(max_threads=1)
    started = threading.Event()
    release = threading.Event()

    class _Blocker(QRunnable):
        def run(self):
            started.set()
            release.wait(5)

    pool._pool.start(_Blocker())
    assert started.wait(2)  # the single thread is now occupied

    req, st = _staged_request(tmp_path)
    ran = []
    orig_run = _FinalizeTask.run

    def spy_run(self):
        ran.append(self.req.chosen_name)
        return orig_run(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_FinalizeTask, "run", spy_run)
        pool.submit(req)
        assert pool.cancel("drive_001") is True
        release.set()
        pool.wait(3000)

    # tryTake succeeded -- run() (and thus the real compression path) never executed.
    assert ran == []
    assert not st.exists()
    rows = read_rows(str(tmp_path))
    assert rows[0]["status"] == Status.CANCELLED.value


# --- skip compression -------------------------------------------------------


def test_skip_before_compression_keeps_raw_and_promotes(tmp_path, qapp, monkeypatch, fake_run):
    req, st = _staged_request(tmp_path)
    signals = _Signals()
    done = []
    signals.done.connect(lambda d, rows: done.append((d, rows)))
    control = _JobControl()
    control.skip_event.set()
    task = _FinalizeTask(req, signals, control)
    fake_run.when("sha256sum", stdout="deadbeef  x\n")

    task.run()

    assert not fake_run.ran("zstd")  # compression genuinely never ran
    assert len(done) == 1
    final_dir, rows = done[0]
    assert rows[0].status == Status.OK.value  # unrelated to compression outcome
    assert rows[0].raw_image_filename == "drive_001.img"
    assert rows[0].compressed_image_filename == "drive_001.img"  # same file, no .zst
    assert "skipped" in rows[0].notes.lower()
    assert os.path.exists(os.path.join(final_dir, "drive_001.img"))
    assert not os.path.exists(os.path.join(final_dir, "drive_001.img.zst"))


def test_skip_mid_compression_falls_back_to_raw(tmp_path, qapp, monkeypatch, fake_run):
    req, st = _staged_request(tmp_path)
    signals = _Signals()
    done = []
    signals.done.connect(lambda d, rows: done.append((d, rows)))
    control = _JobControl()
    task = _FinalizeTask(req, signals, control)
    fake_run.when("sha256sum", stdout="deadbeef  x\n")

    def fake_compress(*a, should_cancel=None, **k):
        control.skip_event.set()  # "the user clicked Skip while this ran"
        assert should_cancel()
        raise compression_mod.CompressionCancelled()

    monkeypatch.setattr(compression_mod, "compress_and_checksum", fake_compress)

    task.run()

    assert len(done) == 1
    final_dir, rows = done[0]
    assert rows[0].compressed_image_filename == "drive_001.img"
    assert os.path.exists(os.path.join(final_dir, "drive_001.img"))
