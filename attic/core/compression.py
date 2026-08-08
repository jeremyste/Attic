"""Compress a raw disk image with the ``zstd`` CLI, then checksum both files.

Policy (the spec): ``zstd -19 --long -T0``, run automatically as soon as a job's
raw image is captured. SHA256 of both the raw and compressed images is recorded.
The ddrescue/gw logfile is always kept uncompressed (never routed through here).
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from . import subprocess_util as su
from .checksums import sha256_file
from .config import COMPRESSED_IMAGE_SUFFIX, ZSTD_LEVEL, ZSTD_LONG, ZSTD_THREADS


class CompressionCancelled(Exception):
    """Raised out of :func:`compress_and_checksum` when ``should_cancel`` fires
    mid-compression. The caller distinguishes a full cancel from a
    skip-compression request by re-checking its own event(s)."""


@dataclass
class CompressionResult:
    raw_path: str
    compressed_path: str
    raw_size_bytes: int
    compressed_size_bytes: int
    sha256_raw: str
    sha256_compressed: str


def build_zstd_argv(
    raw_path: str, out_path: str, *,
    level: int = ZSTD_LEVEL, long_mode: bool = ZSTD_LONG, threads: int = ZSTD_THREADS,
) -> list[str]:
    """Assemble the zstd command line. Kept separate so it is unit-testable."""
    argv = ["zstd", f"-{level}"]
    if long_mode:
        argv.append("--long")
    argv.append(f"-T{threads}")
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
    level: int = ZSTD_LEVEL,
    long_mode: bool = ZSTD_LONG,
    timeout: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
    poll_interval: float = 0.5,
) -> CompressionResult:
    """Compress ``raw_path`` and compute SHA256 of both raw and compressed files.

    ``should_cancel``, if given, is polled while zstd runs (a big HDD image can
    take a long time at ``-19 --long``); when it returns True the process is
    terminated and :class:`CompressionCancelled` is raised, leaving whatever
    partial output zstd had written for the caller to clean up. Without it,
    this runs as a plain blocking call.

    Raises :class:`~attic.core.subprocess_util.CommandError` if zstd fails.
    """
    out_path = out_path or compressed_path_for(raw_path)
    argv = build_zstd_argv(raw_path, out_path, level=level, long_mode=long_mode)

    if should_cancel is None:
        su.run(argv, timeout=timeout, check=True)
    else:
        _run_cancellable(argv, should_cancel, poll_interval)

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


def _run_cancellable(
    argv: list[str], should_cancel: Callable[[], bool], poll_interval: float,
) -> None:
    proc = subprocess.Popen(
        [str(a) for a in argv], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        while proc.poll() is None:
            if should_cancel():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise CompressionCancelled("compression cancelled")
            time.sleep(poll_interval)
    finally:
        proc.wait()
    if proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        from .subprocess_util import CmdResult, CommandError

        raise CommandError(CmdResult(argv=argv, returncode=proc.returncode, stderr=stderr))


def raw_only_result(raw_path: str, *, timeout: float | None = None) -> CompressionResult:
    """A :class:`CompressionResult` for keeping ``raw_path`` itself as the
    archived artifact, uncompressed -- used when compression is skipped.
    "raw" and "compressed" are the same file/bytes here; there is no separate
    compressed copy to point at.
    """
    size = os.path.getsize(raw_path)
    digest = sha256_file(raw_path, timeout=timeout)
    return CompressionResult(
        raw_path=raw_path, compressed_path=raw_path,
        raw_size_bytes=size, compressed_size_bytes=size,
        sha256_raw=digest, sha256_compressed=digest,
    )
