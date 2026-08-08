"""Tests for redoing extraction against an already-archived catalog item."""

from __future__ import annotations

import os

import pytest

import attic.core.reextract as reextract_mod
from attic.core.catalog import CatalogRow, append_row, append_rows, read_rows
from attic.core.extract import ExtractResult
from attic.core.partition import PartitionInfo
from attic.core.reextract import main as reextract_main
from attic.core.reextract import reextract_item


def _make_archive(tmp_path, *, media_type="disc", chosen_name="Classic_ Rock",
                   folder_path="CD/Classic_ Rock", compressed_name="Classic_ Rock.img.zst",
                   fstype="iso9660", status="partial"):
    working = tmp_path
    final_dir = working / folder_path
    final_dir.mkdir(parents=True)
    (final_dir / compressed_name).write_bytes(b"not a real zstd stream, just needs to exist")
    append_row(str(working), CatalogRow(
        media_type=media_type, chosen_name=chosen_name, folder_path=folder_path,
        compressed_image_filename=compressed_name, filesystem_detected=fstype,
        status=status, error_summary="pkexec dismissed",
    ))
    return str(working), str(final_dir)


def test_reextract_no_matching_rows_raises(tmp_path):
    with pytest.raises(ValueError):
        reextract_item(str(tmp_path), "disc", "Nothing Here")


def test_reextract_missing_compressed_image_raises(tmp_path):
    append_row(str(tmp_path), CatalogRow(
        media_type="disc", chosen_name="X", folder_path="CD/X",
        compressed_image_filename="X.img.zst",
    ))
    with pytest.raises(FileNotFoundError):
        reextract_item(str(tmp_path), "disc", "X")


def test_reextract_single_row_updates_catalog(tmp_path, fake_run, monkeypatch):
    working, final_dir = _make_archive(tmp_path)
    fake_run.when("zstd", returncode=0)

    captured = {}

    def fake_extract(image, dest_dir, fstype, *, offset=0, size=0, timeout=None):
        captured["args"] = (dest_dir, fstype, offset, size)
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "movie.mp4"), "w") as fh:
            fh.write("x")
        return ExtractResult(dest_dir=dest_dir, file_count=1, ok=True, notes="7z")

    monkeypatch.setattr(reextract_mod.extract_mod, "extract", fake_extract)

    result = reextract_item(working, "disc", "Classic_ Rock")

    assert result.ok
    assert result.file_count == 1
    assert result.rows_updated == 1
    dest_dir, fstype, offset, size = captured["args"]
    assert dest_dir == os.path.join(final_dir, "Extracted Files")
    assert fstype == "iso9660"
    assert offset == 0 and size == 0

    row = read_rows(working)[0]
    assert row["status"] == "ok"
    assert row["error_summary"] == ""
    assert "reextract" in row["notes"]


def test_reextract_partial_when_nothing_recovered(tmp_path, fake_run, monkeypatch):
    working, _ = _make_archive(tmp_path)
    fake_run.when("zstd", returncode=0)
    monkeypatch.setattr(
        reextract_mod.extract_mod, "extract",
        lambda image, dest_dir, fstype, **k: ExtractResult(
            dest_dir=dest_dir, file_count=0, ok=False, error_summary="still nothing",
        ),
    )

    result = reextract_item(working, "disc", "Classic_ Rock")
    assert not result.ok
    assert result.file_count == 0
    row = read_rows(working)[0]
    assert row["status"] == "failed"
    assert "still nothing" in row["error_summary"]


def test_reextract_redetects_fstype_when_unrecognized(tmp_path, fake_run, monkeypatch):
    working, final_dir = _make_archive(tmp_path, fstype="unrecognized_filesystem")
    fake_run.when("zstd", returncode=0)

    from attic.core.fsdetect import FsDetection

    monkeypatch.setattr(
        reextract_mod.fsdetect, "detect_filesystem",
        lambda image, mount_probe=None: FsDetection(
            fstype="vfat", label="", recognized=True, method="test",
        ),
    )
    captured = {}

    def fake_extract(image, dest_dir, fstype, **k):
        captured["fstype"] = fstype
        return ExtractResult(dest_dir=dest_dir, file_count=3, ok=True)

    monkeypatch.setattr(reextract_mod.extract_mod, "extract", fake_extract)

    result = reextract_item(working, "disc", "Classic_ Rock")
    assert result.ok
    assert captured["fstype"] == "vfat"


def test_reextract_multi_partition_zips_rows_to_partitions_by_position(
    tmp_path, fake_run, monkeypatch,
):
    working = tmp_path
    final_dir = working / "HDD" / "Maxtor 80GB"
    (final_dir / "Partition 1 - System").mkdir(parents=True)
    (final_dir / "Partition 2 - Data").mkdir(parents=True)
    (final_dir / "Maxtor 80GB.img.zst").write_bytes(b"x")

    append_rows(str(working), [
        CatalogRow(media_type="hdd", chosen_name="Maxtor 80GB",
                   folder_path="HDD/Maxtor 80GB", compressed_image_filename="Maxtor 80GB.img.zst",
                   partition_label="System", filesystem_detected="ntfs", status="partial"),
        CatalogRow(media_type="hdd", chosen_name="Maxtor 80GB",
                   folder_path="HDD/Maxtor 80GB", compressed_image_filename="Maxtor 80GB.img.zst",
                   partition_label="Data", filesystem_detected="ntfs", status="partial"),
    ])
    fake_run.when("zstd", returncode=0)
    monkeypatch.setattr(
        reextract_mod, "enumerate_partitions",
        lambda image_path: [
            PartitionInfo(number=1, start=32256, size=1000, fstype_hint="ntfs", name="", flags=""),
            PartitionInfo(number=2, start=2000000, size=3000, fstype_hint="ntfs", name="", flags=""),
        ],
    )
    seen = []

    def fake_extract(image, dest_dir, fstype, *, offset=0, size=0, timeout=None):
        seen.append((dest_dir, offset, size))
        return ExtractResult(dest_dir=dest_dir, file_count=5, ok=True)

    monkeypatch.setattr(reextract_mod.extract_mod, "extract", fake_extract)

    result = reextract_item(str(working), "hdd", "Maxtor 80GB")
    assert result.ok
    assert result.file_count == 10
    assert result.rows_updated == 2
    dests = {os.path.basename(d) for d, _o, _s in seen}
    assert dests == {"Partition 1 - System", "Partition 2 - Data"}
    by_dest = {os.path.basename(d): (o, s) for d, o, s in seen}
    assert by_dest["Partition 1 - System"] == (32256, 1000)
    assert by_dest["Partition 2 - Data"] == (2000000, 3000)


def test_reextract_cli_wrong_arg_count(capsys):
    assert reextract_main([]) == 2
    assert reextract_main(["a", "b"]) == 2


def test_reextract_cli_success_path(tmp_path, fake_run, monkeypatch, capsys):
    working, _ = _make_archive(tmp_path)
    fake_run.when("zstd", returncode=0)
    monkeypatch.setattr(
        reextract_mod.extract_mod, "extract",
        lambda image, dest_dir, fstype, **k: ExtractResult(
            dest_dir=dest_dir, file_count=2, ok=True,
        ),
    )
    rc = reextract_main([working, "disc", "Classic_ Rock"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 file(s) recovered" in out
