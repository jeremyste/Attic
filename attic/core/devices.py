"""Enumerate block devices for the HDD-tab dropdown source.

HARD SAFETY REQUIREMENT (the spec): by default only removable / USB-attached whole
disks are offered, so a mis-click can never target the running OS disk. A device
is *eligible* only if:

  - it is a whole disk (``type == "disk"``), and
  - it is USB-attached (``tran == "usb"``) or flagged removable/hotplug, and
  - none of its partitions (or itself) is mounted at a system path
    (``/``, ``/boot``, ``/boot/efi``, ``/home``, or provides swap), and
  - it isn't the drive currently hosting the working folder's archive (an
    external archive drive is just as removable/USB as a genuine capture
    target, so it needs its own exclusion -- see ``archive_disk_path``).

Every disk is parsed and tagged (``removable`` / ``has_system_mount`` /
``hosts_archive`` / ``eligible``); the default listing keeps only eligible ones.
An explicit override (``include_ineligible=True`` / :func:`list_all_devices`)
also surfaces non-eligible disks — for the case where a genuine target drive in
an enclosure mis-reports its transport as internal, or the rare case of wanting
to image the archive drive itself — so the UI can show them behind a warning
rather than hiding them entirely. Data comes from ``lsblk -J -O`` (JSON, all
columns); parsing is split out so it is unit-testable without real hardware.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import subprocess_util as su

# Mountpoints that mark a device as system-critical and therefore off-limits.
_SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/home", "[SWAP]"}


@dataclass
class BlockDevice:
    name: str  # e.g. "sdb"
    path: str  # e.g. "/dev/sdb"
    model: str
    size: str  # human-readable, as lsblk reports (e.g. "465.8G")
    transport: str  # "usb", "sata", ...
    removable: bool = True  # USB-attached or flagged removable/hotplug
    has_system_mount: bool = False  # hosts /, /boot, /home, swap, ...
    hosts_archive: bool = False  # currently hosts the working folder's archive

    @property
    def eligible(self) -> bool:
        """Safe to offer by default (removable, not a live system disk, and not
        the drive the archive itself lives on)."""
        return self.removable and not self.has_system_mount and not self.hosts_archive

    @property
    def warning(self) -> str:
        """Why this device is not eligible, or '' when it is safe by default."""
        if self.has_system_mount:
            return "currently hosts a mounted system filesystem (likely this PC's own disk)"
        if self.hosts_archive:
            return "this is the drive hosting the archive's working folder"
        if not self.removable:
            return "does not report as removable/USB (may be an internal drive)"
        return ""

    @property
    def label(self) -> str:
        """Human-friendly dropdown label: model + size + path, flagged if unsafe."""
        model = self.model.strip() or "Unknown model"
        base = f"{model} — {self.size} ({self.path})"
        return base if self.eligible else f"⚠ {base}"


def _truthy(value) -> bool:
    """lsblk booleans may be JSON true/false, "1"/"0", or None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip() in ("1", "true", "True")
    return False


def _mountpoints(node: dict) -> list[str]:
    """All mountpoints for a node (lsblk uses 'mountpoints' list on newer, or
    'mountpoint' scalar on older versions)."""
    mps = node.get("mountpoints")
    if isinstance(mps, list):
        return [m for m in mps if m]
    mp = node.get("mountpoint")
    return [mp] if mp else []


def _has_system_mount(node: dict) -> bool:
    """True if this node or any descendant is mounted at a system path/swap."""
    for mp in _mountpoints(node):
        if mp in _SYSTEM_MOUNTS:
            return True
    for child in node.get("children", []) or []:
        if _has_system_mount(child):
            return True
    return False


def _is_removable_or_usb(node: dict) -> bool:
    transport = (node.get("tran") or "").lower()
    if transport == "usb":
        return True
    return _truthy(node.get("rm")) or _truthy(node.get("hotplug"))


def parse_lsblk(
    json_text: str, *, include_ineligible: bool = False, archive_disk_path: str = "",
) -> list[BlockDevice]:
    """Parse ``lsblk -J -O`` output into whole-disk devices.

    By default only *eligible* (removable, non-system, non-archive) disks are
    returned. With ``include_ineligible=True`` every whole disk is returned,
    each tagged with its ``removable``/``has_system_mount``/``hosts_archive``/
    ``eligible`` flags so the UI can warn. ``archive_disk_path`` (e.g.
    ``/dev/sdb``, from :func:`host_disk_path`) marks the matching disk as
    hosting the archive; blank matches nothing.
    """
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return []

    devices: list[BlockDevice] = []
    for node in data.get("blockdevices", []) or []:
        if node.get("type") != "disk":
            continue
        name = node.get("name", "")
        path = node.get("path") or f"/dev/{name}"
        device = BlockDevice(
            name=name,
            path=path,
            model=(node.get("model") or "").strip(),
            size=node.get("size") or "",
            transport=(node.get("tran") or "").lower(),
            removable=_is_removable_or_usb(node),
            has_system_mount=_has_system_mount(node),
            hosts_archive=bool(archive_disk_path) and path == archive_disk_path,
        )
        if device.eligible or include_ineligible:
            devices.append(device)
    return devices


def host_disk_path(path: str, *, timeout: float | None = 10) -> str:
    """The whole-disk device (e.g. ``/dev/sdb``) hosting the filesystem
    containing ``path``, or ``""`` if it can't be determined.

    Used to exclude the archive's own drive from the capture-target dropdown:
    an external archive drive is just as removable/USB as a genuine target, so
    the transport-based filter alone can't tell them apart.
    """
    found = su.run(["findmnt", "-no", "SOURCE", "--target", path], timeout=timeout)
    if not found.ok:
        return ""
    source = found.stdout.strip().splitlines()[0] if found.stdout.strip() else ""
    if not source.startswith("/dev/"):
        return ""
    parent = su.run(["lsblk", "-no", "PKNAME", source], timeout=timeout)
    pkname = parent.stdout.strip().splitlines()[0] if parent.ok and parent.stdout.strip() else ""
    return f"/dev/{pkname}" if pkname else source


def list_removable_devices(
    *, timeout: float | None = 10, archive_disk_path: str = "",
) -> list[BlockDevice]:
    """Run lsblk and return eligible removable/USB whole disks (empty on error)."""
    result = su.run(["lsblk", "-J", "-O"], timeout=timeout)  # -J JSON, -O all columns
    if not result.ok:
        return []
    return parse_lsblk(result.stdout, archive_disk_path=archive_disk_path)


def list_all_devices(
    *, timeout: float | None = 10, archive_disk_path: str = "",
) -> list[BlockDevice]:
    """Run lsblk and return ALL whole disks, tagged (override; empty on error).

    Used by the HDD tab's "show all drives" override for the case where a real
    target drive in an enclosure mis-reports as internal, or a deliberate
    re-image of the archive drive itself. Non-eligible disks are flagged so the
    UI surfaces the risk before any read.
    """
    result = su.run(["lsblk", "-J", "-O"], timeout=timeout)
    if not result.ok:
        return []
    return parse_lsblk(result.stdout, include_ineligible=True, archive_disk_path=archive_disk_path)
