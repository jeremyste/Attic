"""Listing archived HDD drives and deleting a fully-clean one's image."""

from __future__ import annotations

import os

from attic.core.catalog import CatalogRow, append_rows, read_rows
from attic.core.hdd_archive import delete_hdd_image, list_hdd_items


def _row(**kw) -> CatalogRow:
    base = dict(
        media_type="hdd", chosen_name="drive_001", folder_path="HDD/drive_001",
        status="ok", compressed_image_filename="drive_001.img.zst",
        compressed_size_bytes="12345", read_bad_bytes="0",
    )
    base.update(kw)
    return CatalogRow(**base)


def _make_image(tmp_path, folder_path, filename, content=b"x" * 100):
    d = tmp_path / folder_path
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_bytes(content)


# --- list_hdd_items ------------------------------------------------------


def test_single_clean_drive_is_deletable(tmp_path):
    append_rows(str(tmp_path), [_row()])
    items = list_hdd_items(str(tmp_path))
    assert len(items) == 1
    item = items[0]
    assert item.chosen_name == "drive_001"
    assert item.all_extracted_ok
    assert item.read_clean
    assert item.deletable


def test_multi_partition_drive_groups_into_one_item(tmp_path):
    append_rows(str(tmp_path), [
        _row(partition_label="Partition 1"),
        _row(partition_label="Partition 2"),
    ])
    items = list_hdd_items(str(tmp_path))
    assert len(items) == 1
    assert items[0].partition_count == 2


def test_any_non_ok_partition_blocks_deletion(tmp_path):
    append_rows(str(tmp_path), [
        _row(partition_label="Partition 1", status="ok"),
        _row(partition_label="Partition 2", status="partial"),
    ])
    item = list_hdd_items(str(tmp_path))[0]
    assert not item.all_extracted_ok
    assert not item.deletable
    assert "partition" in item.not_deletable_reason.lower()


def test_nonzero_bad_bytes_blocks_deletion(tmp_path):
    append_rows(str(tmp_path), [_row(read_bad_bytes="4096")])
    item = list_hdd_items(str(tmp_path))[0]
    assert not item.read_clean
    assert not item.deletable


def test_unknown_read_quality_is_not_treated_as_clean(tmp_path):
    # Older rows captured before read_bad_bytes was tracked -- blank, not "0".
    append_rows(str(tmp_path), [_row(read_bad_bytes="")])
    item = list_hdd_items(str(tmp_path))[0]
    assert not item.read_clean
    assert not item.deletable


def test_no_image_archived_blocks_deletion(tmp_path):
    append_rows(str(tmp_path), [_row(compressed_image_filename="")])
    item = list_hdd_items(str(tmp_path))[0]
    assert not item.deletable
    assert "no image" in item.not_deletable_reason.lower()


def test_already_deleted_image_is_not_deletable_again(tmp_path):
    append_rows(str(tmp_path), [_row(image_deleted_at="2026-08-08T10:00:00")])
    item = list_hdd_items(str(tmp_path))[0]
    assert item.image_deleted
    assert not item.deletable


def test_non_hdd_rows_are_excluded(tmp_path):
    append_rows(str(tmp_path), [
        _row(),
        CatalogRow(media_type="floppy", chosen_name="floppy_001", status="ok"),
    ])
    items = list_hdd_items(str(tmp_path))
    assert len(items) == 1
    assert items[0].chosen_name == "drive_001"


# --- delete_hdd_image ------------------------------------------------------


def test_delete_removes_file_and_records_timestamp(tmp_path):
    _make_image(tmp_path, "HDD/drive_001", "drive_001.img.zst")
    append_rows(str(tmp_path), [_row()])
    item = list_hdd_items(str(tmp_path))[0]

    result = delete_hdd_image(str(tmp_path), item, timestamp="2026-08-08T12:00:00")

    assert result.ok
    assert result.freed_bytes == 100
    assert not os.path.exists(tmp_path / "HDD/drive_001/drive_001.img.zst")
    row = read_rows(str(tmp_path))[0]
    assert row["image_deleted_at"] == "2026-08-08T12:00:00"
    assert "deleted" in row["notes"].lower()


def test_delete_refuses_when_not_deletable(tmp_path):
    _make_image(tmp_path, "HDD/drive_001", "drive_001.img.zst")
    append_rows(str(tmp_path), [_row(status="partial")])
    item = list_hdd_items(str(tmp_path))[0]

    result = delete_hdd_image(str(tmp_path), item, timestamp="2026-08-08T12:00:00")

    assert not result.ok
    assert os.path.exists(tmp_path / "HDD/drive_001/drive_001.img.zst")
    row = read_rows(str(tmp_path))[0]
    assert row["image_deleted_at"] == ""


def test_delete_updates_all_partition_rows(tmp_path):
    _make_image(tmp_path, "HDD/drive_001", "drive_001.img.zst")
    append_rows(str(tmp_path), [
        _row(partition_label="Partition 1"),
        _row(partition_label="Partition 2"),
    ])
    item = list_hdd_items(str(tmp_path))[0]

    result = delete_hdd_image(str(tmp_path), item, timestamp="2026-08-08T12:00:00")

    assert result.ok
    rows = read_rows(str(tmp_path))
    assert len(rows) == 2
    assert all(r["image_deleted_at"] == "2026-08-08T12:00:00" for r in rows)


def test_delete_preserves_existing_notes(tmp_path):
    _make_image(tmp_path, "HDD/drive_001", "drive_001.img.zst")
    append_rows(str(tmp_path), [_row(notes="tsk_recover fallback used")])
    item = list_hdd_items(str(tmp_path))[0]

    delete_hdd_image(str(tmp_path), item, timestamp="2026-08-08T12:00:00")

    row = read_rows(str(tmp_path))[0]
    assert "tsk_recover fallback used" in row["notes"]
    assert "deleted" in row["notes"].lower()
