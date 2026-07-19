"""SHA256 checksums via the ``sha256sum`` CLI (shelled out, not reimplemented)."""

from __future__ import annotations

from . import subprocess_util as su


def sha256_file(path: str, *, timeout: float | None = None) -> str:
    """Return the hex SHA256 of ``path``.

    Raises :class:`~attic.core.subprocess_util.CommandError` on failure so the
    caller can record it in the catalog's ``error_summary``.
    """
    result = su.run(["sha256sum", "--", path], timeout=timeout, check=True)
    # `sha256sum` prints "<hex>  <filename>"; take the first whitespace token.
    return result.stdout.split(None, 1)[0]
