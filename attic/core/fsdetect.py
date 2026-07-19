"""Filesystem + volume-label detection, shared by all three pipelines.

The same agnostic chain runs against a floppy image, each HDD partition, and an
optical image (the pipelines differ only in the *first* label-source call —
mtools/blkid for floppies+HDD, the ISO9660 volume-id for optical). Do NOT assume
FAT: these span DOS through Windows XP era, and one drive's partitions may differ
from each other.

Chain (Task.md):
    1. blkid (and mtools mlabel for FAT) — most reliable when it works
    2. ``file -s`` signature check against the raw image if inconclusive
    3. attempt a loopback mount with a short candidate list
    4. if nothing recognizes it: unrecognized_filesystem — keep only the
       compressed raw image, never abort the pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import subprocess_util as su
from .config import CANDIDATE_MOUNT_FSTYPES, FAT_FSTYPES, UNRECOGNIZED_FS_LABEL


@dataclass
class FsDetection:
    """Result of detecting filesystem type + label for one volume/image."""

    fstype: str  # normalized fs type, or UNRECOGNIZED_FS_LABEL
    label: str  # volume/partition label, or "" if none
    recognized: bool  # True when a filesystem was identified
    method: str  # which step resolved it (for logs/debugging)

    @property
    def is_fat(self) -> bool:
        return self.fstype.lower() in FAT_FSTYPES


def _blkid(image: str) -> tuple[str, str]:
    """Return ``(fstype, label)`` from blkid, blanks if it finds nothing."""
    # -o export gives stable KEY=VALUE lines (TYPE=, LABEL=).
    result = su.run(["blkid", "-o", "export", "--", image])
    if not result.ok:
        return "", ""
    fields = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
    return fields.get("TYPE", ""), fields.get("LABEL", "")


def _mlabel(image: str) -> str:
    """FAT volume label via mtools ``mlabel`` (empty if none/unsupported)."""
    # `mlabel -i <image> -s ::` prints "Volume label is XYZ" or "No volume label".
    result = su.run(["mlabel", "-i", image, "-s", "::"])
    if not result.ok:
        return ""
    m = re.search(r"Volume label is ([^\r\n]+?)(?:\s+\(|$)", result.stdout)
    return m.group(1).strip() if m else ""


def _file_signature(image: str) -> str:
    """Best-effort fstype guess from ``file -s`` output. '' if unclear."""
    result = su.run(["file", "-s", "-b", "--", image])
    if not result.ok:
        return ""
    text = result.stdout.lower()
    # Order matters: check more specific signatures before generic ones.
    signatures = [
        ("ntfs", "ntfs"),
        ("fat (12", "vfat"),
        ("fat (16", "vfat"),
        ("fat (32", "vfat"),
        ("fat12", "vfat"),
        ("fat16", "vfat"),
        ("fat32", "vfat"),
        ("iso 9660", "iso9660"),
        ("iso9660", "iso9660"),
        ("ext2", "ext2"),
        ("ext3", "ext3"),
        ("ext4", "ext4"),
        ("dos/mbr", "vfat"),
        ("boot sector", "vfat"),
    ]
    for needle, fstype in signatures:
        if needle in text:
            return fstype
    return ""


def _try_candidate_mounts(image: str, mount_probe) -> str:
    """Try mounting ``image`` with each candidate fstype; return the first that works.

    ``mount_probe(image, fstype) -> bool`` performs a real (loopback) mount test;
    injected so this function stays testable and so the real mount lives in one
    place (extract.py owns actual mounting).
    """
    for fstype in CANDIDATE_MOUNT_FSTYPES:
        if mount_probe(image, fstype):
            return fstype
    return ""


def detect_filesystem(
    image: str,
    *,
    initial_label: str = "",
    mount_probe=None,
) -> FsDetection:
    """Run the full detection chain against ``image``.

    ``initial_label`` is a label already obtained by a pipeline-specific call
    (e.g. the ISO9660 volume id for optical); it seeds the label but detection
    still confirms the fstype. ``mount_probe`` enables step 3; if omitted, step 3
    is skipped (used where a real mount isn't available/desired).
    """
    label = initial_label.strip()

    # Step 1: blkid, plus mlabel for a FAT label if blkid gave a type but no label.
    fstype, blkid_label = _blkid(image)
    if blkid_label and not label:
        label = blkid_label
    if fstype:
        if not label and fstype.lower() in FAT_FSTYPES:
            label = _mlabel(image)
        return FsDetection(fstype=fstype, label=label, recognized=True, method="blkid")

    # A FAT label can appear even when blkid is quiet on old media.
    if not label:
        mlabel = _mlabel(image)
        if mlabel:
            label = mlabel

    # Step 2: file signature.
    sig = _file_signature(image)
    if sig:
        if not label and sig in FAT_FSTYPES:
            label = _mlabel(image)
        return FsDetection(fstype=sig, label=label, recognized=True, method="file-s")

    # Step 3: candidate loopback mounts.
    if mount_probe is not None:
        mounted = _try_candidate_mounts(image, mount_probe)
        if mounted:
            return FsDetection(fstype=mounted, label=label, recognized=True, method="mount-probe")

    # Step 4: give up — keep the raw image only, do not abort.
    return FsDetection(
        fstype=UNRECOGNIZED_FS_LABEL, label=label, recognized=False, method="none"
    )
