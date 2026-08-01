"""The finalize pool archives the flux master alongside the sector image.

Runs real zstd + sha256sum against small files, so it pins the on-disk layout a
finished floppy ends up with: extracted tree, compressed image, compressed flux.
"""

from __future__ import annotations

import os

import pytest

from attic.core.config import MediaType


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _run(pool, qapp):
    for _ in range(200):
        qapp.processEvents()
        if pool.wait(50):
            break
    qapp.processEvents()


def _finalize(wf, st, qapp, **kwargs):
    from attic.controllers.compress_pool import FinalizePool, FinalizeRequest
    from attic.core.catalog import CatalogRow

    pool = FinalizePool()
    rows = [CatalogRow(media_type="floppy", chosen_name="Disk1", status="ok")]
    pool.submit(FinalizeRequest(
        working_folder=wf, media_type=MediaType.FLOPPY, staging=st,
        chosen_name="Disk1", rows=rows, zstd_level=1, **kwargs,
    ))
    _run(pool, qapp)
    return rows[0]


def _staged(tmp_path):
    from attic.core import staging
    from attic.ui.session import Session

    wf = str(tmp_path)
    Session(wf).ensure_skeleton()
    return wf, staging.create_staging(wf, MediaType.FLOPPY, "s1")


def test_image_and_flux_are_both_archived(tmp_path, qapp):
    wf, st = _staged(tmp_path)
    with open(st.child("floppy.img"), "wb") as f:
        f.write(b"IMG" * 2048)
    with open(st.child("floppy.scp"), "wb") as f:
        f.write(b"FLUX" * 8192)

    row = _finalize(
        wf, st, qapp,
        raw_image_path=st.child("floppy.img"),
        flux_path=st.child("floppy.scp"),
    )

    final = os.path.join(wf, "Floppy", "Disk1")
    assert os.path.isfile(os.path.join(final, "Disk1.img.zst"))
    assert os.path.isfile(os.path.join(final, "Disk1.scp.zst"))
    # Both uncompressed masters are dropped; the flux one unconditionally,
    # because it dwarfs everything else in the archive.
    assert not os.path.exists(os.path.join(final, "Disk1.img"))
    assert not os.path.exists(os.path.join(final, "Disk1.scp"))

    assert row.compressed_image_filename == "Disk1.img.zst"
    assert row.flux_filename == "Disk1.scp.zst"
    assert row.flux_raw_size_bytes == str(4 * 8192)
    assert int(row.flux_compressed_size_bytes) > 0
    assert len(row.sha256_flux_raw) == 64
    assert len(row.sha256_flux_compressed) == 64


def test_flux_alone_still_archives_when_nothing_decoded(tmp_path, qapp):
    wf, st = _staged(tmp_path)
    with open(st.child("floppy.scp"), "wb") as f:
        f.write(b"FLUX" * 8192)

    row = _finalize(wf, st, qapp, raw_image_path="", flux_path=st.child("floppy.scp"))

    final = os.path.join(wf, "Floppy", "Disk1")
    assert os.path.isfile(os.path.join(final, "Disk1.scp.zst"))
    assert row.flux_filename == "Disk1.scp.zst"
    assert row.compressed_image_filename == ""
    assert row.status == "ok"  # promoted, not recorded as a failure


def test_image_alone_is_unaffected_by_the_flux_plumbing(tmp_path, qapp):
    wf, st = _staged(tmp_path)
    with open(st.child("floppy.img"), "wb") as f:
        f.write(b"IMG" * 2048)

    row = _finalize(wf, st, qapp, raw_image_path=st.child("floppy.img"))

    final = os.path.join(wf, "Floppy", "Disk1")
    assert os.path.isfile(os.path.join(final, "Disk1.img.zst"))
    assert row.compressed_image_filename == "Disk1.img.zst"
    assert row.flux_filename == ""
