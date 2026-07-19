"""Expand a recognized filesystem image into an ``Extracted Files/`` directory.

Two strategies:
  - FAT family -> mtools ``mcopy`` (no mount, no privileges needed).
  - everything else -> loopback ``mount`` (read-only) + copy + ``umount``,
    both wrapped in ``pkexec``.

Unrecognized filesystems are never passed here — the caller skips extraction and
keeps only the compressed raw image.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from . import subprocess_util as su
from .config import FAT_FSTYPES


@dataclass
class ExtractResult:
    dest_dir: str
    file_count: int
    ok: bool
    error_summary: str = ""


def _count_files(root: str) -> int:
    total = 0
    for _dp, _dn, filenames in os.walk(root):
        total += len(filenames)
    return total


def extract_fat(
    image: str, dest_dir: str, *, offset: int = 0, timeout: float | None = None
) -> ExtractResult:
    """Copy everything out of a FAT image with mtools ``mcopy`` (recursive).

    ``offset`` (bytes) selects a partition within a whole-disk image via mtools'
    ``image@@offset`` syntax; 0 means the image is the filesystem itself.
    """
    os.makedirs(dest_dir, exist_ok=True)
    src = image if not offset else f"{image}@@{offset}"
    # -s recursive, -n no-confirm-overwrite, -m preserve mtimes, -Q quit-on-error
    # off (we want best-effort). Source ``::/`` is the image root via -i.
    result = su.run(
        ["mcopy", "-s", "-n", "-m", "-i", src, "::/", dest_dir],
        timeout=timeout,
    )
    if not result.ok:
        # mcopy is best-effort: partial copies still leave usable files, so we
        # report ok only when the tool succeeded, but never raise.
        return ExtractResult(
            dest_dir=dest_dir, file_count=_count_files(dest_dir),
            ok=False, error_summary=result.error_summary(),
        )
    return ExtractResult(dest_dir=dest_dir, file_count=_count_files(dest_dir), ok=True)


def _mount_probe(image: str, fstype: str, *, timeout: float | None = None) -> bool:
    """Return True if ``image`` mounts read-only as ``fstype`` (then unmount).

    Suitable to pass as ``mount_probe`` to fsdetect.detect_filesystem.
    """
    import tempfile

    mnt = tempfile.mkdtemp(prefix="attic-probe-")
    try:
        res = su.run(
            su.with_pkexec(["mount", "-o", "ro,loop", "-t", fstype, image, mnt]),
            timeout=timeout,
        )
        mounted = res.ok
        if mounted:
            su.run(su.with_pkexec(["umount", mnt]), timeout=timeout)
        return mounted
    finally:
        try:
            os.rmdir(mnt)
        except OSError:
            pass


def _mount_options(offset: int, size: int) -> str:
    opts = "ro,loop"
    if offset:
        opts += f",offset={offset}"
    if size:
        opts += f",sizelimit={size}"
    return opts


def extract_mount(
    image: str, dest_dir: str, fstype: str, *,
    offset: int = 0, size: int = 0, timeout: float | None = None,
) -> ExtractResult:
    """Mount ``image`` read-only, copy its tree into ``dest_dir``, then unmount.

    ``offset``/``size`` (bytes) select a partition within a whole-disk image.
    """
    import tempfile

    os.makedirs(dest_dir, exist_ok=True)
    mnt = tempfile.mkdtemp(prefix="attic-mount-")
    opts = _mount_options(offset, size)
    mount_argv = su.with_pkexec(["mount", "-o", opts, "-t", fstype, image, mnt])
    mounted = su.run(mount_argv, timeout=timeout)
    if not mounted.ok:
        _rmdir_quiet(mnt)
        return ExtractResult(
            dest_dir=dest_dir, file_count=0, ok=False,
            error_summary=mounted.error_summary(),
        )
    try:
        # Copy contents of the mount into dest_dir (not the mountpoint itself).
        for entry in os.listdir(mnt):
            src = os.path.join(mnt, entry)
            dst = os.path.join(dest_dir, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(src, dst)
        return ExtractResult(dest_dir=dest_dir, file_count=_count_files(dest_dir), ok=True)
    except OSError as exc:
        return ExtractResult(
            dest_dir=dest_dir, file_count=_count_files(dest_dir),
            ok=False, error_summary=f"copy error: {exc}",
        )
    finally:
        su.run(su.with_pkexec(["umount", mnt]), timeout=timeout)
        _rmdir_quiet(mnt)


def extract(
    image: str, dest_dir: str, fstype: str, *,
    offset: int = 0, size: int = 0, timeout: float | None = None,
) -> ExtractResult:
    """Dispatch to the right extractor for ``fstype``.

    ``offset``/``size`` (bytes) select a partition within a whole-disk image.
    """
    if fstype.lower() in FAT_FSTYPES:
        return extract_fat(image, dest_dir, offset=offset, timeout=timeout)
    return extract_mount(image, dest_dir, fstype, offset=offset, size=size, timeout=timeout)


def _rmdir_quiet(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass
