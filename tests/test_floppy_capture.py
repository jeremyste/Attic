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
from attic.controllers.floppy import (
    TRACK_CLEAN,
    TRACK_FAILED,
    TRACK_RETRIED,
    FloppyCaptureWorker,
    TrackResult,
    _decode_score,
    _GwRun,
    candidate_formats,
)
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
    request = JobRequest(staging_root="/unused", media_type=MediaType.FLOPPY)
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
    request = JobRequest(staging_root="/unused", media_type=MediaType.FLOPPY)
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
    request = JobRequest(staging_root="/unused", media_type=MediaType.FLOPPY)
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


def test_convert_uses_corrected_format_when_scan_finds_a_short_track(
    monkeypatch, staging,
):
    # Reproduces the real bug: c0.0 scans a sector short, everything else is
    # a uniform 18 -- the convert step must be redone with a fixed format
    # rather than trusting scan's per-track counts to build the .img.
    flux, image = staging.child("floppy.scp"), staging.child("floppy.img")
    read_lines = ["T0.0: IBM MFM (17/17 sectors) from 250kbps"] + [
        f"T{c}.{h}: IBM MFM (18/18 sectors) from 250kbps"
        for c in range(80) for h in (0, 1)
        if not (c == 0 and h == 0)
    ]
    worker, seen = _flux_worker(monkeypatch, staging, [
        (read_lines, _write(flux, 4096)),
        (["Found 2878 sectors of 2880 (99%)"], _write(image)),
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

    worker.capture(staging)

    _read_cmd, convert_cmd = seen
    assert convert_cmd[convert_cmd.index("--format") + 1] == "ibm.1440"


def test_convert_keeps_scan_when_track_counts_are_already_uniform(
    monkeypatch, staging,
):
    flux, image = staging.child("floppy.scp"), staging.child("floppy.img")
    read_lines = [
        f"T{c}.{h}: IBM MFM (18/18 sectors) from 250kbps"
        for c in range(80) for h in (0, 1)
    ]
    worker, seen = _flux_worker(monkeypatch, staging, [
        (read_lines, _write(flux, 4096)),
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

    worker.capture(staging)

    _read_cmd, convert_cmd = seen
    assert convert_cmd[convert_cmd.index("--format") + 1] == "ibm.scan"


# --- multi-format fallback (floppy_format_fallbacks) -------------------------


def test_candidate_formats_orders_primary_first_and_dedupes():
    assert candidate_formats("ibm.scan", "amiga.amigados,atarist.720") == [
        "ibm.scan", "amiga.amigados", "atarist.720",
    ]
    # Case-insensitive de-dup: a fallback that just repeats the primary (in any
    # case) contributes nothing extra.
    assert candidate_formats("IBM.SCAN", "ibm.scan, amiga.amigados") == [
        "IBM.SCAN", "amiga.amigados",
    ]


def test_candidate_formats_blank_primary_falls_back_to_ibm_scan():
    assert candidate_formats("  ", "") == ["ibm.scan"]


def test_candidate_formats_ignores_blank_fallback_entries():
    assert candidate_formats("ibm.scan", " , amiga.amigados ,, ") == [
        "ibm.scan", "amiga.amigados",
    ]


def test_decode_score_counts_sectors_and_clean_tracks():
    run = _GwRun(track_results={
        (0, 0): TrackResult(cyl=0, head=0, status=TRACK_CLEAN, sectors_got=18, sectors_total=18),
        (0, 1): TrackResult(cyl=0, head=1, status=TRACK_FAILED, sectors_got=0, sectors_total=18),
        (1, 0): TrackResult(cyl=1, head=0, status=TRACK_RETRIED, sectors_got=17, sectors_total=18),
    })
    assert _decode_score(run) == (35, 1)  # 18+0+17 sectors, 1 fully-clean track


def _flux_worker_with_fallbacks(monkeypatch, scripts, *, format_fallbacks):
    seen: list[list[str]] = []
    calls = iter(scripts)

    def fake_popen(cmd, **_kwargs):
        seen.append(cmd)
        lines, effect = next(calls)
        if effect:
            effect()
        return _FakeProc(lines)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    request = JobRequest(staging_root="/unused", media_type=MediaType.FLOPPY)
    worker = FloppyCaptureWorker(
        request, capture_flux=True, disk_format="ibm.scan",
        format_fallbacks=format_fallbacks,
    )
    return worker, seen


def test_fallback_format_used_when_primary_decode_fails(monkeypatch, staging):
    flux, image = staging.child("floppy.scp"), staging.child("floppy.img")
    cand0 = staging.child("_decode_0.img")
    cand1 = staging.child("_decode_1.img")
    worker, seen = _flux_worker_with_fallbacks(
        monkeypatch,
        [
            (["T0.0: IBM MFM (18/18 sectors) from 250kbps"], _write(flux, 4096)),
            (["** FATAL ERROR:", "No tracks found"], None),  # ibm.scan: nothing
            (["T0.0: Amiga DOS (11/11 sectors)"], _write(cand1, 512)),  # amiga: works
        ],
        format_fallbacks="amiga.amigados",
    )
    monkeypatch.setattr(
        "attic.controllers.floppy.fsdetect.detect_filesystem",
        lambda *a, **k: _Det(fstype="affs", recognized=True),
    )
    monkeypatch.setattr(
        "attic.controllers.floppy.extract_mod.extract", lambda *a, **k: _ExtractOk()
    )
    monkeypatch.setattr(
        "attic.controllers.floppy.scan_tree_date", lambda *a, **k: _Scan()
    )

    art = worker.capture(staging)

    read_cmd, convert0, convert1 = seen
    assert read_cmd[:2] == ["gw", "read"]
    assert convert0[convert0.index("--format") + 1] == "ibm.scan"
    assert convert1[convert1.index("--format") + 1] == "amiga.amigados"
    # The winning (amiga) candidate's temp file was promoted to the real name,
    # and no per-candidate temp files were left behind.
    assert art.raw_image_path == image
    assert os.path.exists(image)
    assert not os.path.exists(cand0)
    assert not os.path.exists(cand1)
    assert art.status == Status.OK
    assert art.filesystem_detected == "affs"


def test_clean_primary_decode_skips_fallback_candidates(monkeypatch, staging):
    flux, image = staging.child("floppy.scp"), staging.child("floppy.img")
    cand0 = staging.child("_decode_0.img")
    worker, seen = _flux_worker_with_fallbacks(
        monkeypatch,
        [
            (["T0.0: IBM MFM (18/18 sectors) from 250kbps"], _write(flux, 4096)),
            (["T0.0: IBM MFM (18/18 sectors) from 250kbps"], _write(cand0)),
        ],
        format_fallbacks="amiga.amigados,atarist.720",
    )
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

    worker.capture(staging)

    # Only the read + the primary format's convert ran -- a fully clean decode
    # short-circuits the (otherwise wasted) fallback attempts.
    assert len(seen) == 2
    assert os.path.exists(image)
    assert not os.path.exists(cand0)


def test_no_fallbacks_configured_decodes_straight_to_final_name(monkeypatch, staging):
    """format_fallbacks="" (the pre-feature behavior) writes directly to
    floppy.img, same as before this feature existed -- no rename/temp file."""
    flux, image = staging.child("floppy.scp"), staging.child("floppy.img")
    worker, seen = _flux_worker_with_fallbacks(
        monkeypatch,
        [
            (["T0.0: IBM MFM (18/18 sectors) from 250kbps"], _write(flux, 4096)),
            (["Found 2880 sectors of 2880 (100%)"], _write(image)),
        ],
        format_fallbacks="",
    )
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

    worker.capture(staging)

    assert len(seen) == 2
    convert_cmd = seen[1]
    assert convert_cmd[-2:] == [flux, image]
