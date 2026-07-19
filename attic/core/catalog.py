"""The single ``catalog.csv`` at the working-folder root.

One row per archived volume (one row *per partition* for multi-partition HDDs,
linked by a shared ``source_id`` + ``sequence_number``). Appends are serialized
with a module-level lock because the three pipelines can finish and write rows at
nearly the same instant.
"""

from __future__ import annotations

import csv
import os
import threading
from dataclasses import asdict, dataclass, fields
from typing import Iterable

from .config import CATALOG_FILENAME

# Exact column order of the catalog CSV (matches the spec schema).
COLUMNS = [
    "timestamp",
    "media_type",
    "sequence_number",
    "source_id",
    "physical_label_entered",
    "detected_label",
    "chosen_name",
    "partition_label",
    "folder_path",
    "fallback_date_used",
    "filesystem_detected",
    "raw_image_filename",
    "compressed_image_filename",
    "raw_size_bytes",
    "compressed_size_bytes",
    "sha256_raw",
    "sha256_compressed",
    "error_summary",
    "status",
    "notes",
]

# Serializes all appends across threads/pipelines within a process.
_write_lock = threading.Lock()


@dataclass
class CatalogRow:
    """One catalog row. Field names/order mirror :data:`COLUMNS` exactly."""

    timestamp: str = ""
    media_type: str = ""
    sequence_number: str = ""
    source_id: str = ""
    physical_label_entered: str = ""
    detected_label: str = ""
    chosen_name: str = ""
    partition_label: str = ""
    folder_path: str = ""
    fallback_date_used: str = ""
    filesystem_detected: str = ""
    raw_image_filename: str = ""
    compressed_image_filename: str = ""
    raw_size_bytes: str = ""
    compressed_size_bytes: str = ""
    sha256_raw: str = ""
    sha256_compressed: str = ""
    error_summary: str = ""
    status: str = ""
    notes: str = ""

    def as_ordered(self) -> dict[str, str]:
        d = asdict(self)
        return {col: _to_str(d[col]) for col in COLUMNS}


# Guard: dataclass fields must stay in sync with COLUMNS.
assert [f.name for f in fields(CatalogRow)] == COLUMNS, (
    "CatalogRow fields out of sync with COLUMNS"
)


def _to_str(value) -> str:
    return "" if value is None else str(value)


def catalog_path(working_folder: str) -> str:
    return os.path.join(working_folder, CATALOG_FILENAME)


def ensure_catalog(working_folder: str) -> str:
    """Create ``catalog.csv`` with a header row if it does not yet exist.

    Returns the catalog path. Safe to call repeatedly.
    """
    path = catalog_path(working_folder)
    with _write_lock:
        if not os.path.exists(path):
            os.makedirs(working_folder, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=COLUMNS).writeheader()
    return path


def append_row(working_folder: str, row: CatalogRow) -> None:
    """Append a single row, creating the file+header if needed. Thread-safe."""
    append_rows(working_folder, [row])


def append_rows(working_folder: str, rows: Iterable[CatalogRow]) -> None:
    """Append multiple rows atomically w.r.t. other appends. Thread-safe.

    Useful for a multi-partition HDD so its rows land contiguously.
    """
    rows = list(rows)
    if not rows:
        return
    path = catalog_path(working_folder)
    with _write_lock:
        exists = os.path.exists(path)
        os.makedirs(working_folder, exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            if not exists:
                writer.writeheader()
            for row in rows:
                writer.writerow(row.as_ordered())


def read_rows(working_folder: str) -> list[dict[str, str]]:
    """Return all rows as dicts (empty list if the catalog does not exist)."""
    path = catalog_path(working_folder)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def rename_item(
    working_folder: str, media_type: str, old_name: str, new_name: str, *,
    old_folder_path: str, new_folder_path: str,
) -> int:
    """Rewrite the catalog, renaming every row of one item. Thread-safe.

    Matches rows by ``media_type`` + ``chosen_name == old_name`` (covers all
    partition rows of a multi-partition drive) and updates their ``chosen_name``
    and ``folder_path``. Returns the number of rows changed.
    """
    path = catalog_path(working_folder)
    changed = 0
    with _write_lock:
        if not os.path.exists(path):
            return 0
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            if row.get("media_type") == media_type and row.get("chosen_name") == old_name:
                row["chosen_name"] = new_name
                if row.get("folder_path") == old_folder_path:
                    row["folder_path"] = new_folder_path
                changed += 1
        if changed:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
    return changed


def highest_sequence(working_folder: str, media_type: str) -> int:
    """Highest ``sequence_number`` recorded for ``media_type`` (0 if none).

    Scans the catalog rather than keeping a counter file, so resuming a folder
    across sessions continues numbering correctly. Non-integer/blank sequence
    values are ignored.
    """
    highest = 0
    for row in read_rows(working_folder):
        if row.get("media_type") != media_type:
            continue
        raw = (row.get("sequence_number") or "").strip()
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        highest = max(highest, n)
    return highest


def existing_chosen_names(working_folder: str, media_type: str) -> set[str]:
    """Set of ``chosen_name`` values already used within a media type.

    Dedup is scoped per media type (Floppy/, HDD/, CD/ are separate namespaces).
    """
    return {
        (row.get("chosen_name") or "")
        for row in read_rows(working_folder)
        if row.get("media_type") == media_type and row.get("chosen_name")
    }
