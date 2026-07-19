"""Name resolution for an archived volume.

Priority (Task.md step 4):
    1. physical label entered by the user
    2. detected volume/partition label
    3. auto-generated fallback ``{prefix}_{NNN}_{date}``

All three source values (physical_label_entered, detected_label, chosen_name)
are preserved in the returned result and recorded in the catalog regardless of
which one won — they frequently disagree, and that disagreement is useful.

Dedup is scoped per media type (Floppy/, HDD/, CD/ are independent namespaces):
on collision we append ``_2``, ``_3`` ... to the sanitized chosen name.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import catalog
from .config import MediaType
from .sanitize import sanitize_filename


@dataclass
class NameResolution:
    """Result of resolving a name. All fields are recorded in the catalog."""

    chosen_name: str  # final, sanitized, de-duplicated folder name
    physical_label_entered: str
    detected_label: str
    sequence_number: int  # sequence assigned for this media type
    used_fallback: bool  # True if neither physical nor detected label applied
    fallback_date: str  # the date component used in the fallback (or "")


def build_fallback_name(
    media_type: MediaType, sequence_number: int, date_str: str
) -> str:
    """Compose ``{prefix}_{NNN}_{date}`` (date omitted if unknown)."""
    prefix = media_type.fallback_prefix
    base = f"{prefix}_{sequence_number:03d}"
    return f"{base}_{date_str}" if date_str else base


def dedupe_name(name: str, taken: set[str]) -> str:
    """Return ``name`` or the first free ``name_2``, ``name_3`` ... variant."""
    if name not in taken:
        return name
    n = 2
    while f"{name}_{n}" in taken:
        n += 1
    return f"{name}_{n}"


def resolve_name(
    working_folder: str,
    media_type: MediaType,
    *,
    physical_label: str = "",
    detected_label: str = "",
    fallback_date: str = "",
    extra_taken: set[str] | None = None,
) -> NameResolution:
    """Resolve the final folder/chosen name for one volume.

    ``extra_taken`` lets callers reserve names not yet in the catalog (e.g. the
    other partitions of the same drive being processed in one batch), so a batch
    doesn't produce internal collisions before any row is written.
    """
    physical = (physical_label or "").strip()
    detected = (detected_label or "").strip()

    sequence_number = catalog.highest_sequence(working_folder, media_type.value) + 1

    used_fallback = False
    if physical:
        base = physical
    elif detected:
        base = detected
    else:
        base = build_fallback_name(media_type, sequence_number, fallback_date)
        used_fallback = True

    sanitized = sanitize_filename(base)

    taken = catalog.existing_chosen_names(working_folder, media_type.value)
    if extra_taken:
        taken = taken | extra_taken
    chosen = dedupe_name(sanitized, taken)

    return NameResolution(
        chosen_name=chosen,
        physical_label_entered=physical,
        detected_label=detected,
        sequence_number=sequence_number,
        used_fallback=used_fallback,
        fallback_date=fallback_date if used_fallback else "",
    )
