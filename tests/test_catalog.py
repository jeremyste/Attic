import csv
import threading

import pytest

from attic.core.catalog import (
    COLUMNS,
    CatalogRow,
    append_row,
    append_rows,
    ensure_catalog,
    existing_chosen_names,
    highest_sequence,
    read_rows,
    update_row,
)


def test_ensure_catalog_writes_header(tmp_path):
    path = ensure_catalog(str(tmp_path))
    with open(path, newline="") as fh:
        header = next(csv.reader(fh))
    assert header == COLUMNS


def test_row_field_order_matches_columns():
    # Guard against schema drift between the dataclass and the CSV columns.
    row = CatalogRow(timestamp="t", media_type="floppy", chosen_name="Docs")
    ordered = list(row.as_ordered().keys())
    assert ordered == COLUMNS


def test_append_and_read(tmp_path):
    append_row(
        str(tmp_path),
        CatalogRow(media_type="disc", sequence_number="1", chosen_name="Backup"),
    )
    rows = read_rows(str(tmp_path))
    assert len(rows) == 1
    assert rows[0]["media_type"] == "disc"
    assert rows[0]["chosen_name"] == "Backup"


def test_highest_sequence_scoped_per_media_type(tmp_path):
    append_rows(
        str(tmp_path),
        [
            CatalogRow(media_type="floppy", sequence_number="1"),
            CatalogRow(media_type="floppy", sequence_number="4"),
            CatalogRow(media_type="disc", sequence_number="9"),
            CatalogRow(media_type="floppy", sequence_number=""),  # ignored
        ],
    )
    assert highest_sequence(str(tmp_path), "floppy") == 4
    assert highest_sequence(str(tmp_path), "disc") == 9
    assert highest_sequence(str(tmp_path), "hdd") == 0


def test_highest_sequence_no_catalog(tmp_path):
    assert highest_sequence(str(tmp_path), "floppy") == 0


def test_existing_chosen_names_scoped(tmp_path):
    append_rows(
        str(tmp_path),
        [
            CatalogRow(media_type="floppy", chosen_name="Docs"),
            CatalogRow(media_type="disc", chosen_name="Docs"),
            CatalogRow(media_type="floppy", chosen_name="Games"),
        ],
    )
    assert existing_chosen_names(str(tmp_path), "floppy") == {"Docs", "Games"}
    assert existing_chosen_names(str(tmp_path), "disc") == {"Docs"}


def test_multi_partition_rows_share_identifier(tmp_path):
    append_rows(
        str(tmp_path),
        [
            CatalogRow(
                media_type="hdd", source_id="/dev/sdb", sequence_number="1",
                partition_label="System", chosen_name="System",
            ),
            CatalogRow(
                media_type="hdd", source_id="/dev/sdb", sequence_number="1",
                partition_label="Data", chosen_name="Data",
            ),
        ],
    )
    rows = read_rows(str(tmp_path))
    assert len(rows) == 2
    assert {r["partition_label"] for r in rows} == {"System", "Data"}
    assert {r["source_id"] for r in rows} == {"/dev/sdb"}
    assert {r["sequence_number"] for r in rows} == {"1"}


def test_concurrent_appends_are_serialized(tmp_path):
    # 20 threads each appending one row must yield exactly 20 well-formed rows.
    def worker(i):
        append_row(str(tmp_path), CatalogRow(media_type="floppy", sequence_number=str(i)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = read_rows(str(tmp_path))
    assert len(rows) == 20
    assert {r["sequence_number"] for r in rows} == {str(i) for i in range(20)}


def test_older_catalog_header_is_widened_on_append(tmp_path):
    """Columns are append-only, so an older file must be upgraded, not overflowed."""
    import csv as _csv

    from attic.core.catalog import COLUMNS, CatalogRow, append_row, read_rows

    old_cols = COLUMNS[:-5]  # header as written before the flux columns existed
    path = tmp_path / "catalog.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=old_cols)
        w.writeheader()
        w.writerow({c: "" for c in old_cols} | {"chosen_name": "OldDisk", "status": "ok"})

    append_row(str(tmp_path), CatalogRow(
        chosen_name="NewDisk", status="ok", flux_filename="NewDisk.scp.zst",
    ))

    rows = read_rows(str(tmp_path))
    assert [r["chosen_name"] for r in rows] == ["OldDisk", "NewDisk"]
    # The pre-existing row survives intact and gains empty new columns...
    assert rows[0]["status"] == "ok"
    assert rows[0]["flux_filename"] == ""
    # ...and the new value is reachable by name rather than stranded past the end.
    assert rows[1]["flux_filename"] == "NewDisk.scp.zst"
    assert None not in rows[1]


def test_update_row_changes_matching_rows(tmp_path):
    append_rows(
        str(tmp_path),
        [
            CatalogRow(media_type="disc", chosen_name="Classic_ Rock", status="partial",
                       error_summary="pkexec dismissed"),
            CatalogRow(media_type="floppy", chosen_name="Classic_ Rock", status="ok"),
        ],
    )
    changed = update_row(
        str(tmp_path), "disc", "Classic_ Rock",
        status="ok", error_summary="", notes="reextract: 42 file(s)",
    )
    assert changed == 1
    rows = read_rows(str(tmp_path))
    disc_row = next(r for r in rows if r["media_type"] == "disc")
    floppy_row = next(r for r in rows if r["media_type"] == "floppy")
    assert disc_row["status"] == "ok"
    assert disc_row["error_summary"] == ""
    assert disc_row["notes"] == "reextract: 42 file(s)"
    # A same-named row of a *different* media type must be untouched.
    assert floppy_row["status"] == "ok"
    assert floppy_row["notes"] == ""


def test_update_row_multiple_matches_all_updated(tmp_path):
    # Multi-partition HDD: several rows share one chosen_name.
    append_rows(
        str(tmp_path),
        [
            CatalogRow(media_type="hdd", chosen_name="Maxtor 80GB", partition_label="System",
                       status="partial"),
            CatalogRow(media_type="hdd", chosen_name="Maxtor 80GB", partition_label="Data",
                       status="partial"),
        ],
    )
    changed = update_row(str(tmp_path), "hdd", "Maxtor 80GB", status="ok")
    assert changed == 2
    assert {r["status"] for r in read_rows(str(tmp_path))} == {"ok"}


def test_update_row_no_match_returns_zero(tmp_path):
    append_rows(str(tmp_path), [CatalogRow(media_type="disc", chosen_name="Other")])
    assert update_row(str(tmp_path), "disc", "Nonexistent", status="ok") == 0


def test_update_row_no_catalog_returns_zero(tmp_path):
    assert update_row(str(tmp_path), "disc", "X", status="ok") == 0


def test_update_row_rejects_unknown_column(tmp_path):
    append_rows(str(tmp_path), [CatalogRow(media_type="disc", chosen_name="X")])
    with pytest.raises(ValueError):
        update_row(str(tmp_path), "disc", "X", not_a_real_column="oops")


def test_unrecognized_header_is_left_alone(tmp_path):
    import csv as _csv

    from attic.core.catalog import CatalogRow, append_row

    path = tmp_path / "catalog.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=["something", "unexpected"])
        w.writeheader()
        w.writerow({"something": "a", "unexpected": "b"})
    before = path.read_text()

    append_row(str(tmp_path), CatalogRow(chosen_name="X", status="ok"))

    # The foreign rows must still be there verbatim; we never rewrite what we
    # do not understand.
    assert path.read_text().startswith(before)
