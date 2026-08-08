"""Expand a recognized filesystem image into an ``Extracted Files/`` directory.

A tiered, best-effort recovery chain, not a single strategy:

  1. The fstype-appropriate structured extractor -- mtools ``mcopy`` for FAT
     (no mount, no privileges), kernel ``mount`` (read-only, via the
     privileged helper) + copy for everything else. Fastest and most
     faithful (real names/paths/permissions) when the directory structure is
     intact.
  2. ``tsk_recover`` (The Sleuthkit) -- a different, more tolerant parser for
     the same filesystem families (FAT/NTFS/ext/ISO9660/UDF), needs no
     privilege at all. Tried whenever tier 1 came up short: a
     corrupted-but-not-destroyed directory structure that trips up one parser
     may not trip up the other.
  2b. ``7z`` (optical only) -- an independent ISO9660/Joliet/UDF reader,
      often more tolerant of damaged/hybrid/multisession discs than a mount.
  3. ``photorec`` -- signature-based carving. Ignores the directory/FAT/MFT
     structure entirely and recovers file *content* by scanning for known
     file-type headers -- the only tier that can do anything once that
     structure itself is destroyed, not just awkward to parse. Output goes to
     a separate ``<dest> (carved)/`` directory rather than merging into the
     structured result: carved files have generic names (original
     filenames/paths are unrecoverable) and some will be false positives or
     truncated, so it's kept visibly apart from a trustworthy extraction.

Every tier keeps whatever it produced even when it doesn't fully succeed --
recovering some files beats recovering none. ``ExtractResult.notes`` records
which tier(s) actually ran and what each contributed.

Unrecognized filesystems are never passed here — the caller skips extraction and
keeps only the compressed raw image.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from dataclasses import dataclass

from . import subprocess_util as su
from .config import FAT_FSTYPES

OPTICAL_FSTYPES = ("iso9660", "udf")

# photorec's non-interactive batch grammar (verified against a real damaged
# floppy image): "wholespace" skips the interactive partition-selection
# screen and carves the whole input as one region; "fileopt,everything,enable"
# turns on every signature it knows; "search" is what actually starts the run.
_PHOTOREC_CMD = "wholespace,fileopt,everything,enable,search"


@dataclass
class ExtractResult:
    dest_dir: str
    file_count: int
    ok: bool
    error_summary: str = ""
    notes: str = ""


def _count_files(root: str) -> int:
    total = 0
    for _dp, _dn, filenames in os.walk(root):
        total += len(filenames)
    return total


def _is_enospc(exc: BaseException) -> bool:
    """True for a real ENOSPC, whether raised directly or buried inside a
    ``shutil.Error`` (whose per-file messages are strings, not exceptions --
    that's the shape a ``copytree`` failure comes in as)."""
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return True
    return "no space left on device" in str(exc).lower()


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _rmdir_quiet(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


# --- Tier 1: structured, fstype-specific ------------------------------------


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
    count = _count_files(dest_dir)
    if not result.ok:
        return ExtractResult(
            dest_dir=dest_dir, file_count=count,
            ok=False, error_summary=result.error_summary(),
        )
    if count == 0:
        # mcopy exiting 0 only means it didn't hit a hard error -- an empty
        # result from a filesystem that isn't genuinely empty (physical
        # damage to the FAT/root-directory area is the common cause) looks
        # identical unless we check. Flag it rather than reporting a clean
        # "ok" for a volume nothing was actually recovered from.
        return ExtractResult(
            dest_dir=dest_dir, file_count=0, ok=False,
            error_summary=(
                "mcopy reported success but extracted 0 files "
                "(FAT/root-directory area likely damaged)"
            ),
        )
    return ExtractResult(dest_dir=dest_dir, file_count=count, ok=True)


def _mount_probe(image: str, fstype: str, *, timeout: float | None = None) -> bool:
    """Return True if ``image`` mounts read-only as ``fstype`` (then unmount).

    Suitable to pass as ``mount_probe`` to fsdetect.detect_filesystem.
    """
    mnt = tempfile.mkdtemp(prefix="attic-probe-")
    try:
        res = su.run_privileged(
            ["mount", "-o", "ro,loop", "-t", fstype, image, mnt], timeout=timeout,
        )
        mounted = res.ok
        if mounted:
            su.run_privileged(["umount", mnt], timeout=timeout)
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
    A real ``ENOSPC`` stops the copy immediately (continuing is guaranteed
    pointless) and records the destination's actual free space at that
    instant; any other per-entry failure is skipped so the rest of the tree
    still gets a chance, rather than one bad entry losing everything after it.
    """
    os.makedirs(dest_dir, exist_ok=True)
    mnt = tempfile.mkdtemp(prefix="attic-mount-")
    opts = _mount_options(offset, size)
    mount_argv = ["mount", "-o", opts, "-t", fstype, image, mnt]
    mounted = su.run_privileged(mount_argv, timeout=timeout)
    if not mounted.ok:
        _rmdir_quiet(mnt)
        return ExtractResult(
            dest_dir=dest_dir, file_count=0, ok=False,
            error_summary=mounted.error_summary(),
        )

    errors: list[str] = []
    try:
        for entry in sorted(os.listdir(mnt)):
            src = os.path.join(mnt, entry)
            dst = os.path.join(dest_dir, entry)
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
                else:
                    shutil.copy2(src, dst)
            except OSError as exc:
                if _is_enospc(exc):
                    usage = shutil.disk_usage(dest_dir)
                    errors.append(
                        f"out of space copying {entry!r}: destination filesystem had "
                        f"{usage.free} of {usage.total} bytes free at that instant "
                        f"-- stopped rather than continuing a copy that can't finish"
                    )
                    break
                errors.append(f"{entry!r}: {exc}")
                continue

        count = _count_files(dest_dir)
        if not errors and count == 0:
            errors.append(
                "mount succeeded but extraction produced 0 files "
                "(directory structure likely damaged)"
            )
        return ExtractResult(
            dest_dir=dest_dir, file_count=count, ok=not errors and count > 0,
            error_summary="; ".join(errors),
        )
    finally:
        su.run_privileged(["umount", mnt], timeout=timeout)
        _rmdir_quiet(mnt)


# --- Tier 2: independent structured parser (all fstypes) --------------------


def extract_tsk(
    image: str, dest_dir: str, *, offset: int = 0, timeout: float | None = None
) -> ExtractResult:
    """Recover files via The Sleuthkit's ``tsk_recover``.

    A different, more tolerant, independent parser for the same filesystem
    families tier 1 handles -- reads the image file directly, no root/mount
    needed. Tries allocated files first (``-a``); if that comes up empty, a
    follow-up pass also recovers deleted/unallocated entries (``-e``), since
    the goal here is maximum recovery, not a clean listing.

    ``offset`` (bytes) is converted to sectors assuming 512-byte sectors,
    which matches every image this app produces (floppy/HDD/optical, all
    standard 512-byte-sector media).
    """
    if not su.has_tool("tsk_recover"):
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False,
                              error_summary="tsk_recover not installed")
    os.makedirs(dest_dir, exist_ok=True)
    base_argv = ["tsk_recover"]
    if offset:
        base_argv += ["-o", str(offset // 512)]

    result = su.run([*base_argv, "-a", image, dest_dir], timeout=timeout)
    count = _count_files(dest_dir)
    tried_deleted = False
    if count == 0:
        tried_deleted = True
        result = su.run([*base_argv, "-e", image, dest_dir], timeout=timeout)
        count = _count_files(dest_dir)

    if count == 0:
        return ExtractResult(
            dest_dir=dest_dir, file_count=0, ok=False,
            error_summary=result.error_summary() or "tsk_recover found nothing",
        )
    notes = "tsk_recover -e (including deleted/unallocated entries)" if tried_deleted else "tsk_recover -a"
    return ExtractResult(dest_dir=dest_dir, file_count=count, ok=True, notes=notes)


def extract_7z(
    image: str, dest_dir: str, *, timeout: float | None = None
) -> ExtractResult:
    """Extract an ISO9660/Joliet/UDF image via 7z.

    Unprivileged, no mount -- an independent reader that's often more
    tolerant of damaged/hybrid/multisession discs than the kernel's mount.
    """
    if not su.has_tool("7z"):
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False,
                              error_summary="7z not installed")
    os.makedirs(dest_dir, exist_ok=True)
    result = su.run(["7z", "x", f"-o{dest_dir}", "-y", "--", image], timeout=timeout)
    count = _count_files(dest_dir)
    if count == 0:
        return ExtractResult(
            dest_dir=dest_dir, file_count=0, ok=False,
            error_summary=result.error_summary() or "7z extracted nothing",
        )
    return ExtractResult(dest_dir=dest_dir, file_count=count, ok=True, notes="7z")


# --- Tier 3: last resort, filesystem-agnostic carving ------------------------


def _slice_image(image: str, offset: int, size: int) -> str:
    """Copy ``size`` bytes at ``offset`` from ``image`` into a temp file.

    Only needed for photorec, which has no CLI notion of "just this
    partition" -- everything else in this module can address a byte offset
    directly (mount's ``offset=``/``sizelimit=``, tsk_recover's ``-o``).
    """
    fd, path = tempfile.mkstemp(prefix="attic-slice-", suffix=".img")
    with os.fdopen(fd, "wb") as out, open(image, "rb") as src:
        src.seek(offset)
        remaining = size
        while remaining > 0:
            chunk = src.read(min(4 * 1024 * 1024, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)
    return path


def extract_photorec(
    image: str, dest_dir: str, *,
    offset: int = 0, size: int = 0, timeout: float | None = None,
) -> ExtractResult:
    """Signature-based file carving via photorec -- the last resort.

    Recovers file *content* by scanning for known file-type headers,
    completely independent of the directory/FAT/MFT structure -- the only
    tier that can do anything once that structure itself is destroyed.
    Output lands directly in ``dest_dir`` (the caller is expected to pass a
    location clearly separate from a structured extraction, e.g. a
    ``<name> (carved)`` sibling directory) with generic filenames
    (``f0000136.jpg``-style) -- original names/paths are unrecoverable, and
    some carved files will be false positives or truncated.
    """
    if not su.has_tool("photorec"):
        return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False,
                              error_summary="photorec not installed")

    sliced_path = ""
    target = image
    if offset or size:
        slice_size = size or (os.path.getsize(image) - offset)
        sliced_path = _slice_image(image, offset, slice_size)
        target = sliced_path

    work_base = tempfile.mkdtemp(prefix="attic-carve-")
    photorec_dest = os.path.join(work_base, "out")
    try:
        result = su.run(
            [
                "photorec", "/log", "/d", photorec_dest, "/cmd", target,
                _PHOTOREC_CMD,
            ],
            input_text="",  # never let it wait on an interactive prompt
            timeout=timeout,
        )
        produced = photorec_dest + ".1"
        if not os.path.isdir(produced):
            return ExtractResult(
                dest_dir=dest_dir, file_count=0, ok=False,
                error_summary=result.error_summary() or "photorec produced no output",
            )
        os.makedirs(dest_dir, exist_ok=True)
        for entry in os.listdir(produced):
            if entry == "report.xml":
                continue
            shutil.move(os.path.join(produced, entry), os.path.join(dest_dir, entry))

        count = _count_files(dest_dir)
        notes = (
            "carved by content signature, not filesystem structure -- original "
            "filenames/paths are lost and some files may be false positives or truncated"
        )
        if count == 0:
            return ExtractResult(dest_dir=dest_dir, file_count=0, ok=False,
                                  error_summary="photorec found nothing", notes=notes)
        return ExtractResult(dest_dir=dest_dir, file_count=count, ok=True, notes=notes)
    finally:
        shutil.rmtree(work_base, ignore_errors=True)
        if sliced_path:
            _remove_quiet(sliced_path)


# --- Dispatch: the tiered recovery chain -------------------------------------


def _carved_dir_for(dest_dir: str) -> str:
    """Sibling location for tier-3 carved output, distinct per ``dest_dir``."""
    return f"{dest_dir.rstrip(os.sep)} (carved)"


def extract(
    image: str, dest_dir: str, fstype: str, *,
    offset: int = 0, size: int = 0, timeout: float | None = None,
) -> ExtractResult:
    """Recover as much as possible from ``image``, trying progressively more
    tolerant strategies until one yields real files. See module docstring.
    """
    is_fat = fstype.lower() in FAT_FSTYPES
    is_optical_fs = fstype.lower() in OPTICAL_FSTYPES

    if is_fat:
        primary = extract_fat(image, dest_dir, offset=offset, timeout=timeout)
    else:
        primary = extract_mount(
            image, dest_dir, fstype, offset=offset, size=size, timeout=timeout
        )

    if primary.ok and primary.file_count > 0:
        return primary

    tiers_tried = [f"tier1 ({fstype})"]
    best_count = primary.file_count

    tsk = extract_tsk(image, dest_dir, offset=offset, timeout=timeout)
    tiers_tried.append("tsk_recover")
    best_count = max(best_count, tsk.file_count)

    if is_optical_fs:
        seven = extract_7z(image, dest_dir, timeout=timeout)
        tiers_tried.append("7z")
        best_count = max(best_count, seven.file_count)

    if best_count > 0:
        return ExtractResult(
            dest_dir=dest_dir, file_count=_count_files(dest_dir), ok=True,
            error_summary=(
                f"tier1 ({fstype}) recovered {primary.file_count} file(s); "
                f"recovered more via: {', '.join(tiers_tried[1:])}"
            ),
            notes=f"tiers tried: {', '.join(tiers_tried)}",
        )

    # Tier 3: last resort, filesystem-agnostic carving into a separate folder.
    carve_dir = _carved_dir_for(dest_dir)
    carved = extract_photorec(image, carve_dir, offset=offset, size=size, timeout=timeout)
    tiers_tried.append("photorec")
    if carved.file_count > 0:
        return ExtractResult(
            dest_dir=carve_dir, file_count=carved.file_count, ok=True,
            error_summary=(
                f"no structured extraction recovered anything (tried "
                f"{', '.join(tiers_tried[:-1])}); recovered {carved.file_count} "
                f"file(s) via signature-based carving instead -- see {carve_dir!r}"
            ),
            notes=carved.notes,
        )

    return ExtractResult(
        dest_dir=dest_dir, file_count=0, ok=False,
        error_summary=(
            f"no files recovered after trying {', '.join(tiers_tried)}: "
            f"{primary.error_summary}"
        ),
    )
