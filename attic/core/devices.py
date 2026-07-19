"""Enumerate block devices safe to image — the HDD-tab dropdown source.

HARD SAFETY REQUIREMENT (Task.md): only removable / USB-attached whole disks may
be offered. The system's own internal/boot drives must be filtered out entirely,
so a mis-click can never target the running OS disk. A device qualifies only if:

  - it is a whole disk (``type == "disk"``), and
  - it is USB-attached (``tran == "usb"``) or flagged removable/hotplug, and
  - none of its partitions (or itself) is mounted at a system path
    (``/``, ``/boot``, ``/boot/efi``, ``/home``, or provides swap).

The last check is belt-and-suspenders: even a USB disk currently hosting a system
mount is excluded. Data comes from ``lsblk -J -O`` (JSON, all columns); parsing is
split out so it is unit-testable against captured JSON without real hardware.
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

    @property
    def label(self) -> str:
        """Human-friendly dropdown label: model + size + path."""
        model = self.model.strip() or "Unknown model"
        return f"{model} — {self.size} ({self.path})"


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


def parse_lsblk(json_text: str) -> list[BlockDevice]:
    """Parse ``lsblk -J -O`` output into the list of eligible devices."""
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return []

    devices: list[BlockDevice] = []
    for node in data.get("blockdevices", []) or []:
        if node.get("type") != "disk":
            continue
        if not _is_removable_or_usb(node):
            continue  # internal SATA/NVMe boot disk -> excluded
        if _has_system_mount(node):
            continue  # currently hosting a system mount -> excluded
        name = node.get("name", "")
        devices.append(
            BlockDevice(
                name=name,
                path=node.get("path") or f"/dev/{name}",
                model=(node.get("model") or "").strip(),
                size=node.get("size") or "",
                transport=(node.get("tran") or "").lower(),
            )
        )
    return devices


def list_removable_devices(*, timeout: float | None = 10) -> list[BlockDevice]:
    """Run lsblk and return eligible removable/USB whole disks (empty on error)."""
    result = su.run(["lsblk", "-J", "-O"], timeout=timeout)  # -J JSON, -O all columns
    if not result.ok:
        return []
    return parse_lsblk(result.stdout)
