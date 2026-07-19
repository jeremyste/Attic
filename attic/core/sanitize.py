"""NTFS-safe filename sanitization.

The destination drive the user hands back is NTFS-formatted, so chosen names and
partition-label folder names must be valid on NTFS regardless of what the source
volume allowed. We target NTFS/Windows rules (a strict superset of what ext4
needs), namely:

- forbidden characters ``< > : " / \\ | ? *`` and control chars (0x00-0x1F),
- no trailing spaces or dots,
- reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9), case-insensitive,
  optionally followed by an extension.
"""

from __future__ import annotations

import re

# Characters NTFS forbids in a path component, plus ASCII control characters.
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows reserved device names (with or without an extension).
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

_DEFAULT_FALLBACK = "unnamed"


def sanitize_filename(name: str, *, replacement: str = "_") -> str:
    """Return an NTFS-safe version of ``name``.

    Forbidden characters are replaced with ``replacement``; trailing spaces/dots
    are stripped; reserved device names are suffixed with ``replacement`` so they
    are no longer reserved. Never returns an empty string.
    """
    if name is None:
        return _DEFAULT_FALLBACK

    # Replace forbidden characters.
    cleaned = _FORBIDDEN_RE.sub(replacement, name)

    # Collapse whitespace runs to single spaces and trim, then strip trailing
    # dots/spaces which NTFS silently drops (and which cause surprising names).
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(" .")

    if not cleaned:
        return _DEFAULT_FALLBACK

    # Neutralize reserved device names (compare on the stem before the first dot).
    stem = cleaned.split(".", 1)[0]
    if stem.upper() in _RESERVED_NAMES:
        cleaned = f"{stem}{replacement}" + cleaned[len(stem):]

    return cleaned


def is_reserved(name: str) -> bool:
    """True if ``name``'s stem is a Windows reserved device name."""
    stem = name.split(".", 1)[0]
    return stem.upper() in _RESERVED_NAMES
