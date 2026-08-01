"""Failure handling for the gw read step.

Verified against a real Greaseweazle V4.1 (host tools 1.23, firmware 1.6): with
no disk in the drive, ``gw read`` prints "Command Failed: GetFluxStatus: No
Index", writes no image file at all, and still **exits 0**. Trusting the exit
code alone would promote an empty capture as a successful one, so these tests
pin the output-scanning + image-existence checks that catch it.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from attic.controllers.base import JobRequest
from attic.controllers.floppy import FloppyCaptureWorker
from attic.core.config import MediaType, Status
from attic.core.staging import StagingDir


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = iter(line + "\n" for line in lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode

    def terminate(self):
        pass


@pytest.fixture
def staging(tmp_path):
    st = StagingDir(str(tmp_path), MediaType.FLOPPY, "sess1")
    os.makedirs(st.path, exist_ok=True)
    return st


def _worker(monkeypatch, lines, returncode=0, **kw):
    """A worker whose gw invocation replays ``lines``. Returns (worker, captured argv).

    Defaults to the direct (no-flux) read path; pass ``capture_flux=True`` for
    the flux-first path.
    """
    kw.setdefault("capture_flux", False)
    seen: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        seen.append(cmd)
        return _FakeProc(lines, returncode)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    request = JobRequest(working_folder="/unused", media_type=MediaType.FLOPPY)
    return FloppyCaptureWorker(request, **kw), seen


def test_no_disk_is_failed_despite_exit_zero(monkeypatch, staging):
    worker, seen = _worker(monkeypatch, [
        "Reading c=0-81:h=0-1 revs=3",
        "Format ibm.scan",
        "Command Failed: GetFluxStatus: No Index",
    ], returncode=0)

    art = worker.capture(staging)

    assert art.status == Status.FAILED
    assert "No Index" in art.error_summary
    assert seen[0][:2] == ["gw", "read"]
    # No image was written, so detection must not have been attempted.
    assert not os.path.exists(art.raw_image_path)


def test_fatal_error_message_is_carried_into_the_summary(monkeypatch, staging):
    worker, _ = _worker(monkeypatch, [
        "Reading c=0-81:h=0-1 revs=3",
        "** FATAL ERROR:",
        "Sector image requires a disk format to be specified",
    ], returncode=1)

    art = worker.capture(staging)

    assert art.status == Status.FAILED
    assert art.error_summary == "Sector image requires a disk format to be specified"


def test_missing_image_fails_even_when_tracks_were_reported(monkeypatch, staging):
    worker, _ = _worker(monkeypatch, [
        "T0.0: IBM MFM (18/18 sectors) from 250kbps, 300rpm",
        "Command Failed: Lost sync",
    ], returncode=0)

    art = worker.capture(staging)
    assert art.status == Status.FAILED


def test_retry_lines_for_one_track_count_as_a_single_bad_track(monkeypatch, staging):
    # gw reprints the same track on every retry; the failed-track tally must not
    # multiply-count it. Here the retries eventually succeed, so the read is OK.
    worker, _ = _worker(monkeypatch, [
        "T5.0: IBM MFM (0/18 sectors) from 250kbps (Retry #0.1)",
        "T5.0: IBM MFM (0/18 sectors) from 250kbps (Retry #0.2)",
        "T5.0: IBM MFM (18/18 sectors) from 250kbps (Retry #0.3)",
    ])
    monkeypatch.setattr(
        "attic.controllers.floppy.fsdetect.detect_filesystem",
        lambda *a, **k: _Det(),
    )
    with open(staging.child("floppy.img"), "wb") as fh:
        fh.write(b"\0" * 512)

    art = worker.capture(staging)
    assert art.status == Status.UNRECOGNIZED_FS  # i.e. not PARTIAL from the read


def test_partial_when_a_track_is_unrecoverable(monkeypatch, staging):
    worker, _ = _worker(monkeypatch, [
        "T5.0: IBM MFM (0/18 sectors) from 250kbps",
        "T5.0: Giving up: 18 sectors missing",
    ])
    # A recognized filesystem, so the read's own PARTIAL verdict is what survives.
    monkeypatch.setattr(
        "attic.controllers.floppy.fsdetect.detect_filesystem",
        lambda *a, **k: _Det(fstype="vfat", recognized=True),
    )
    monkeypatch.setattr(
        "attic.controllers.floppy.extract_mod.extract",
        lambda *a, **k: _ExtractOk(),
    )
    monkeypatch.setattr(
        "attic.controllers.floppy.scan_tree_date", lambda *a, **k: _Scan()
    )
    with open(staging.child("floppy.img"), "wb") as fh:
        fh.write(b"\0" * 512)

    art = worker.capture(staging)
    assert art.status == Status.PARTIAL


class _Det:
    """Stand-in for an fsdetect result; recognizes nothing unless told to."""

    def __init__(self, fstype="unrecognized_filesystem", recognized=False):
        self.fstype = fstype
        self.recognized = recognized
        self.label = ""
        self.method = "none"


class _ExtractOk:
    ok = True
    error_summary = ""


class _Scan:
    date_str = ""
    suspect = False


# --- flux-first capture -----------------------------------------------------


def _flux_worker(monkeypatch, staging, scripts):
    """Worker in flux mode; ``scripts`` is a list of (lines, side_effect) per call.

    ``side_effect`` runs when that gw invocation starts, letting a test simulate
    the file each stage would have produced.
    """
    seen: list[list[str]] = []
    calls = iter(scripts)

    def fake_popen(cmd, **_kwargs):
        seen.append(cmd)
        lines, effect = next(calls)
        if effect:
            effect()
        return _FakeProc(lines)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    request = JobRequest(working_folder="/unused", media_type=MediaType.FLOPPY)
    worker = FloppyCaptureWorker(request, capture_flux=True, disk_format="ibm.scan")
    return worker, seen


def _write(path, size=512):
    return lambda: open(path, "wb").write(b"\0" * size)


def test_flux_read_uses_raw_and_keeps_format_for_the_track_grid(monkeypatch, staging):
    flux, image = staging.child("floppy.scp"), staging.child("floppy.img")
    worker, seen = _flux_worker(monkeypatch, staging, [
        (["T0.0: IBM MFM (18/18 sectors) from 250kbps"], _write(flux, 4096)),
        (["Found 2880 sectors of 2880 (100%)"], _write(image)),
    ])
    monkeypatch.setattr(
        "attic.controllers.floppy.fsdetect.detect_filesystem",
        lambda *a, **k: _Det(fstype="vfat", recognized=True),
    )
    monkeypatch.setattr(
        "attic.controllers.floppy.extract_mod.extract", lambda *a, **k: _ExtractOk()
    )
    monkeypatch.setattr(
        "attic.controllers.floppy.scan_tree_date", lambda *a, **k: _Scan()
    )

    art = worker.capture(staging)

    read_cmd, convert_cmd = seen
    # --raw is what makes the .scp a genuine flux master rather than flux
    # re-synthesized from decoded sectors.
    assert "--raw" in read_cmd
    # --format is still passed: it is what produces the per-track sector counts
    # the live grid consumes.
    assert read_cmd[read_cmd.index("--format") + 1] == "ibm.scan"
    assert read_cmd[-1].endswith("floppy.scp")
    assert convert_cmd[:2] == ["gw", "convert"]
    assert convert_cmd[-2:] == [flux, image]

    assert art.status == Status.OK
    assert art.flux_path == flux
    assert art.raw_image_path == image


def test_flux_kept_when_it_decodes_to_nothing(monkeypatch, staging):
    # The whole point of the flux master: an undecodable disk still archives.
    flux = staging.child("floppy.scp")
    worker, _ = _flux_worker(monkeypatch, staging, [
        (["T0.0: IBM MFM (0/18 sectors) from 250kbps"], _write(flux, 4096)),
        (["** FATAL ERROR:", "No tracks found"], None),
    ])

    art = worker.capture(staging)

    assert art.flux_path == flux
    assert art.raw_image_path == ""  # nothing decoded
    assert art.status == Status.UNRECOGNIZED_FS
    assert os.path.exists(flux)


def test_failed_flux_read_archives_nothing(monkeypatch, staging):
    worker, _ = _flux_worker(monkeypatch, staging, [
        (["Command Failed: GetFluxStatus: No Index"], None),
    ])

    art = worker.capture(staging)

    assert art.status == Status.FAILED
    assert art.flux_path == ""
    assert art.raw_image_path == ""


def test_flux_revs_override_is_passed_through(monkeypatch, staging):
    flux = staging.child("floppy.scp")
    seen: list[list[str]] = []

    def fake_popen(cmd, **_kwargs):
        seen.append(cmd)
        open(flux, "wb").write(b"\0" * 16)
        return _FakeProc(["Command Failed: stop here"])

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    request = JobRequest(working_folder="/unused", media_type=MediaType.FLOPPY)
    FloppyCaptureWorker(request, capture_flux=True, flux_revs=3).capture(staging)

    assert seen[0][seen[0].index("--revs") + 1] == "3"


def test_flux_revs_zero_leaves_gw_on_the_format_default(monkeypatch, staging):
    flux = staging.child("floppy.scp")
    worker, seen = _flux_worker(monkeypatch, staging, [
        (["Command Failed: stop here"], _write(flux, 16)),
        (["** FATAL ERROR:", "nope"], None),
    ])
    worker.capture(staging)
    assert "--revs" not in seen[0]
