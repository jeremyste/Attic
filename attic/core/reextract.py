"""Redo extraction for an already-archived catalog item, from its compressed
image, without re-imaging the source.

There is no code path elsewhere that reprocesses a promoted item -- capture ->
finalize -> promote is normally one-way. This exists for exactly the failures
this session turned up: a pkexec prompt got dismissed mid-extraction, a copy
ran out of disk space, or the primary extraction strategy just wasn't
tolerant enough of a damaged filesystem -- in every case the *capture* (raw
image) is already safely archived and only the *extraction* needs another
try, now backed by extract.py's full tiered recovery chain.

Multi-partition HDD items: partition offsets/sizes aren't stored in the
catalog, so they're rediscovered by re-running ``enumerate_partitions``
against the decompressed image and zipping the result back to catalog rows by
position (the same order they were originally appended in). All matching rows
get the same combined status/notes rather than a per-partition update --
adequate for retrying a failed job, not a general partition-level catalog editor.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass

from . import catalog
from . import extract as extract_mod
from . import fsdetect
from . import subprocess_util as su
from .config import EXTRACTED_DIRNAME, Status
from .partition import enumerate_partitions


@dataclass
class ReextractResult:
    chosen_name: str
    rows_updated: int
    file_count: int
    ok: bool
    notes: str = ""


def _decompress(compressed_path: str, dest_path: str, *, timeout: float | None = None) -> None:
    result = su.run(
        ["zstd", "-d", "-f", "-q", "-o", dest_path, "--", compressed_path],
        timeout=timeout,
    )
    if not result.ok:
        raise RuntimeError(f"failed to decompress {compressed_path!r}: {result.error_summary()}")


def _worse(a: Status, b: Status) -> Status:
    order = {Status.OK: 0, Status.PARTIAL: 1, Status.UNRECOGNIZED_FS: 2, Status.FAILED: 3}
    return a if order[a] >= order[b] else b


def reextract_item(
    working_folder: str, media_type: str, chosen_name: str, *, timeout: float | None = None,
) -> ReextractResult:
    """Decompress ``chosen_name``'s archived image and redo extraction into
    its existing folder, merging in whatever the recovery chain finds.
    """
    rows = [
        r for r in catalog.read_rows(working_folder)
        if r.get("media_type") == media_type and r.get("chosen_name") == chosen_name
    ]
    if not rows:
        raise ValueError(f"no catalog rows for ({media_type!r}, {chosen_name!r})")

    compressed_name = rows[0].get("compressed_image_filename") or ""
    if not compressed_name:
        raise ValueError(f"no compressed image recorded for {chosen_name!r}")
    folder_path = rows[0].get("folder_path") or ""
    final_dir = os.path.join(working_folder, folder_path)
    compressed_path = os.path.join(final_dir, compressed_name)
    if not os.path.exists(compressed_path):
        raise FileNotFoundError(compressed_path)

    tmp_root = os.path.join(working_folder, ".tmp")
    scratch = tempfile.mkdtemp(
        prefix="reextract-", dir=tmp_root if os.path.isdir(tmp_root) else None,
    )
    image_path = os.path.join(scratch, "image.img")
    try:
        _decompress(compressed_path, image_path, timeout=timeout)

        # (row, dest_dir, fstype, offset, size)
        targets: list[tuple[dict, str, str, int, int]] = []
        if len(rows) == 1:
            dest_dir = os.path.join(final_dir, EXTRACTED_DIRNAME)
            targets.append((rows[0], dest_dir, rows[0].get("filesystem_detected") or "", 0, 0))
        else:
            partitions = enumerate_partitions(image_path)
            for idx, (row, part) in enumerate(zip(rows, partitions), start=1):
                label = row.get("partition_label") or ""
                folder_name = f"Partition {idx} - {label}" if label else f"Partition {idx}"
                dest_dir = os.path.join(final_dir, folder_name)
                if not os.path.isdir(dest_dir):
                    # The naming guess didn't match what's actually on disk --
                    # skip rather than silently creating a wrongly-named folder.
                    continue
                targets.append((row, dest_dir, row.get("filesystem_detected") or "", part.start, part.size))

        total_files = 0
        note_parts: list[str] = []
        error_parts: list[str] = []
        worst = Status.OK
        for row, dest_dir, fstype, offset, size in targets:
            if not fstype or fstype == "unrecognized_filesystem":
                det = fsdetect.detect_filesystem(image_path, mount_probe=extract_mod._mount_probe)
                fstype = det.fstype
            result = extract_mod.extract(
                image_path, dest_dir, fstype, offset=offset, size=size, timeout=timeout,
            )
            total_files += result.file_count
            label = row.get("partition_label") or chosen_name
            note_parts.append(
                f"{label}: {result.file_count} file(s)"
                + (f" -- {result.notes}" if result.notes else "")
            )
            if result.error_summary:
                error_parts.append(f"{label}: {result.error_summary}")
            row_status = Status.OK if result.ok else (
                Status.PARTIAL if result.file_count > 0 else Status.FAILED
            )
            worst = _worse(worst, row_status)

        combined_notes = (
            f"reextract: {'; '.join(note_parts)}" if note_parts
            else "reextract: nothing to do (no matching folders found)"
        )
        changed = catalog.update_row(
            working_folder, media_type, chosen_name,
            status=worst.value, notes=combined_notes,
            error_summary="; ".join(error_parts),
        )
        return ReextractResult(
            chosen_name=chosen_name, rows_updated=changed, file_count=total_files,
            ok=total_files > 0, notes=combined_notes,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3:
        print(
            "usage: python -m attic.core.reextract <working_folder> <media_type> <chosen_name>",
            file=sys.stderr,
        )
        return 2
    working_folder, media_type, chosen_name = argv
    try:
        result = reextract_item(working_folder, media_type, chosen_name)
    except Exception as exc:  # noqa: BLE001 - CLI boundary, report and exit non-zero
        print(f"reextract failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"{chosen_name}: {result.file_count} file(s) recovered "
        f"({result.rows_updated} catalog row(s) updated)"
    )
    print(result.notes)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
