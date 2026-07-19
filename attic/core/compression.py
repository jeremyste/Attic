"""Compress a raw disk image with the ``zstd`` CLI, then checksum both files.

Policy (Task.md): ``zstd -19 --long -T0``, run automatically as soon as a job's
raw image is captured. SHA256 of both the raw and compressed images is recorded.
The ddrescue/gw logfile is always kept uncompressed (never routed through here).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import subprocess_util as su
from .checksums import sha256_file
from .config import COMPRESSED_IMAGE_SUFFIX, ZSTD_LEVEL, ZSTD_LONG, ZSTD_THREADS


@dataclass
class CompressionResult:
    raw_path: str
    compressed_path: str
    raw_size_bytes: int
    compressed_size_bytes: int
    sha256_raw: str
    sha256_compressed: str


def build_zstd_argv(raw_path: str, out_path: str) -> list[str]:
    """Assemble the zstd command line. Kept separate so it is unit-testable."""
    argv = ["zstd", f"-{ZSTD_LEVEL}"]
    if ZSTD_LONG:
        argv.append("--long")
    argv.append(f"-T{ZSTD_THREADS}")
    # Explicit output path; -f to allow overwrite within our own staging dir;
    # -q to keep stderr for genuine errors only.
    argv += ["-q", "-f", "-o", out_path, "--", raw_path]
    return argv


def compressed_path_for(raw_path: str) -> str:
    """``foo.img`` -> ``foo.img.zst`` (append the zst suffix to the raw name)."""
    if raw_path.endswith(".img"):
        return raw_path[: -len(".img")] + COMPRESSED_IMAGE_SUFFIX
    return raw_path + ".zst"


def compress_and_checksum(
    raw_path: str,
    out_path: str | None = None,
    *,
    timeout: float | None = None,
) -> CompressionResult:
    """Compress ``raw_path`` and compute SHA256 of both raw and compressed files.

    Raises :class:`~attic.core.subprocess_util.CommandError` if zstd fails.
    """
    out_path = out_path or compressed_path_for(raw_path)

    su.run(build_zstd_argv(raw_path, out_path), timeout=timeout, check=True)

    raw_size = os.path.getsize(raw_path)
    comp_size = os.path.getsize(out_path)
    return CompressionResult(
        raw_path=raw_path,
        compressed_path=out_path,
        raw_size_bytes=raw_size,
        compressed_size_bytes=comp_size,
        sha256_raw=sha256_file(raw_path, timeout=timeout),
        sha256_compressed=sha256_file(out_path, timeout=timeout),
    )
