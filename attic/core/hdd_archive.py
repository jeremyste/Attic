"""Manage already-archived HDD items: list them, and free space by deleting a
fully-recovered drive's compressed/raw image once it's no longer needed.

Deletion is only offered when EVERY partition row for a drive shows a clean
read (zero ddrescue bad bytes on the pass that was accepted) and a
successful extraction (status "ok") -- otherwise the image is the only
surviving copy of some of that drive's data, so keeping it is not optional.
Extracted Files/ (Partition N/ subfolders) are never touched; only the whole-
drive image file is removed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import catalog
from .config import Status


@dataclass
class HddArchiveItem:
    """One archived HDD (all its partition rows collapsed into one summary)."""

    chosen_name: str
    folder_path: str
    partition_count: int
    all_extracted_ok: bool  # every partition row status == "ok"
    read_clean: bool  # ddrescue reported 0 bad bytes on the accepted pass
    image_filename: str
    compressed_size_bytes: int
    image_deleted: bool

    @property
    def deletable(self) -> bool:
        return (
            self.all_extracted_ok and self.read_clean and not self.image_deleted
            and bool(self.image_filename)
        )

    @property
    def not_deletable_reason(self) -> str:
        if self.image_deleted:
            return "image already deleted"
        if not self.image_filename:
            return "no image was archived for this drive"
        if not self.read_clean:
            return "read was not fully clean (bad sectors, or read quality unrecorded)"
        if not self.all_extracted_ok:
            return "not every partition extracted cleanly"
        return ""


def list_hdd_items(working_folder: str) -> list[HddArchiveItem]:
    """One :class:`HddArchiveItem` per distinct archived drive, in catalog order."""
    rows = [r for r in catalog.read_rows(working_folder) if r.get("media_type") == "hdd"]

    order: list[str] = []
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        name = row.get("chosen_name") or ""
        if name not in by_name:
            by_name[name] = []
            order.append(name)
        by_name[name].append(row)

    items = []
    for name in order:
        group = by_name[name]
        items.append(_summarize(name, group))
    return items


def _summarize(name: str, group: list[dict]) -> HddArchiveItem:
    all_ok = all(r.get("status") == Status.OK.value for r in group)

    bad_values = [r.get("read_bad_bytes") or "" for r in group]
    # Every partition row shares the same drive-level figure; treat a blank
    # (older rows, captured before this was tracked) as unknown -- unknown is
    # not the same as known-clean, so it must not be deletable.
    read_clean = bool(bad_values) and all(
        v.isdigit() and int(v) == 0 for v in bad_values
    )

    image_filename = next(
        (r.get("compressed_image_filename") or r.get("raw_image_filename") or ""
         for r in group), ""
    )
    image_deleted = any(r.get("image_deleted_at") for r in group)

    try:
        size = int(group[0].get("compressed_size_bytes") or group[0].get("raw_size_bytes") or 0)
    except ValueError:
        size = 0

    return HddArchiveItem(
        chosen_name=name,
        folder_path=group[0].get("folder_path") or "",
        partition_count=len(group),
        all_extracted_ok=all_ok,
        read_clean=read_clean,
        image_filename=image_filename,
        compressed_size_bytes=size,
        image_deleted=image_deleted,
    )


@dataclass
class DeleteImageResult:
    ok: bool
    freed_bytes: int = 0
    error: str = ""


def delete_hdd_image(
    working_folder: str, item: HddArchiveItem, *, timestamp: str,
) -> DeleteImageResult:
    """Delete ``item``'s image file and record when, in the catalog.

    ``timestamp`` is passed in (rather than computed here) so callers -- and
    tests -- control it; this module has no wall-clock dependency.
    """
    if not item.deletable:
        return DeleteImageResult(ok=False, error=f"not eligible: {item.not_deletable_reason}")

    path = os.path.join(working_folder, item.folder_path, item.image_filename)
    freed = 0
    try:
        if os.path.exists(path):
            freed = os.path.getsize(path)
            os.remove(path)
    except OSError as exc:
        return DeleteImageResult(ok=False, error=str(exc))

    existing_notes = ""
    for row in catalog.read_rows(working_folder):
        if row.get("media_type") == "hdd" and row.get("chosen_name") == item.chosen_name:
            existing_notes = row.get("notes") or ""
            break
    note = (
        f"Compressed image ({item.image_filename}) deleted {timestamp} to reclaim "
        f"space -- read and extraction were both fully clean; Extracted Files "
        f"(Partition folders) retained."
    )
    combined_notes = (existing_notes + " | " + note).strip(" |")

    catalog.update_row(
        working_folder, "hdd", item.chosen_name,
        image_deleted_at=timestamp, notes=combined_notes,
    )
    return DeleteImageResult(ok=True, freed_bytes=freed)
