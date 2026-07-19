"""Derive a representative date from a mounted volume's file modification times.

Used by the naming fallback ``{type}_{NNN}_{date}``. We take the most recent
*valid* file mtime on the tree — "valid" meaning within a sane range, because
machines with a dead CMOS battery reset the clock (often to 1970 or 1980, or
occasionally to a bogus future date). Dates outside [MIN_VALID_YEAR, today] are
ignored for the chosen date, and if the raw newest mtime was out of range we
flag the result as suspect so the UI can warn and let the user override.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime

from .config import MIN_VALID_YEAR


@dataclass
class DateScanResult:
    """Outcome of scanning a directory tree for a representative date.

    ``chosen_date`` is the newest in-range mtime (or ``None`` if nothing on the
    volume had a sane date). ``suspect`` is True when the overall newest mtime
    fell outside the valid range (dead-clock symptom) — even if an in-range
    date was still found among older files.
    """

    chosen_date: date | None
    suspect: bool
    files_scanned: int
    newest_raw: date | None  # newest mtime seen, regardless of range

    @property
    def date_str(self) -> str:
        """``YYYY-MM-DD`` for the chosen date, or empty string if none."""
        return self.chosen_date.isoformat() if self.chosen_date else ""


def _in_range(d: date, today: date) -> bool:
    return date(MIN_VALID_YEAR, 1, 1) <= d <= today


def scan_tree_date(root: str, *, today: date | None = None) -> DateScanResult:
    """Walk ``root`` and return the most recent valid file modification date.

    ``today`` defaults to the current date; injectable for deterministic tests.
    Directories are ignored (their mtimes are noise); only regular files count.
    Unreadable entries are skipped rather than raising.
    """
    if today is None:
        today = date.today()

    newest_valid: date | None = None
    newest_raw: date | None = None
    count = 0

    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            count += 1
            d = datetime.fromtimestamp(mtime).date()
            if newest_raw is None or d > newest_raw:
                newest_raw = d
            if _in_range(d, today) and (newest_valid is None or d > newest_valid):
                newest_valid = d

    # Suspect when the newest thing we saw was out of range (dead clock), or when
    # we found no in-range date at all despite having scanned files.
    suspect = False
    if newest_raw is not None and not _in_range(newest_raw, today):
        suspect = True
    if count > 0 and newest_valid is None:
        suspect = True

    return DateScanResult(
        chosen_date=newest_valid,
        suspect=suspect,
        files_scanned=count,
        newest_raw=newest_raw,
    )
