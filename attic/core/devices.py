"""Enumerate block devices for the HDD-tab dropdown source.

HARD SAFETY REQUIREMENT (the spec): by default only removable / USB-attached whole
disks are offered, so a mis-click can never target the running OS disk. A device
is *eligible* only if:

  - it is a whole disk (``type == "disk"``), and
  - it is USB-attached (``tran == "usb"``) or flagged removable/hotplug, and
  - none of its partitions (or itself) is mounted at a system path
    (``/``, ``/boot``, ``/boot/efi``, ``/home``, or provides swap).

Every disk is parsed and tagged (``removable`` / ``has_system_mount`` /
``eligible``); the default listing keeps only eligible ones. An explicit override
(``include_ineligible=True`` / :func:`list_all_devices`) also surfaces
non-eligible disks — for the case where a genuine target drive in an enclosure
mis-reports its transport as internal — so the UI can show them behind a warning
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

    @property
    def eligible(self) -> bool:
        """Safe to offer by default (removable and not a live system disk)."""
        return self.removable and not self.has_system_mount

    @property
    def warning(self) -> str:
        """Why this device is not eligible, or '' when it is safe by default."""
        if self.has_system_mount:
            return "currently hosts a mounted system filesystem (likely this PC's own disk)"
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


def parse_lsblk(json_text: str, *, include_ineligible: bool = False) -> list[BlockDevice]:
    """Parse ``lsblk -J -O`` output into whole-disk devices.

    By default only *eligible* (removable, non-system) disks are returned. With
    ``include_ineligible=True`` every whole disk is returned, each tagged with its
    ``removable``/``has_system_mount``/``eligible`` flags so the UI can warn.
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
        device = BlockDevice(
            name=name,
            path=node.get("path") or f"/dev/{name}",
            model=(node.get("model") or "").strip(),
            size=node.get("size") or "",
            transport=(node.get("tran") or "").lower(),
            removable=_is_removable_or_usb(node),
            has_system_mount=_has_system_mount(node),
        )
        if device.eligible or include_ineligible:
            devices.append(device)
    return devices


def list_removable_devices(*, timeout: float | None = 10) -> list[BlockDevice]:
    """Run lsblk and return eligible removable/USB whole disks (empty on error)."""
    result = su.run(["lsblk", "-J", "-O"], timeout=timeout)  # -J JSON, -O all columns
    if not result.ok:
        return []
    return parse_lsblk(result.stdout)


def list_all_devices(*, timeout: float | None = 10) -> list[BlockDevice]:
    """Run lsblk and return ALL whole disks, tagged (override; empty on error).

    Used by the HDD tab's "show all drives" override for the case where a real
    target drive in an enclosure mis-reports as internal. Non-eligible disks are
    flagged so the UI surfaces the risk before any read.
    """
    result = su.run(["lsblk", "-J", "-O"], timeout=timeout)
    if not result.ok:
        return []
    return parse_lsblk(result.stdout, include_ineligible=True)
