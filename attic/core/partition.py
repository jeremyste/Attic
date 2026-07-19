"""Partition-table enumeration for a captured whole-disk image.

We image the entire drive first, then enumerate partitions *from the image file*
(so nothing touches the physical device after the rescue) using ``parted -m``
machine-readable output. Each partition is then detected/extracted independently
via an offset into the image — do not assume all partitions share a filesystem.
The parsing here is pure and unit-tested; running parted is a subprocess concern.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import subprocess_util as su


@dataclass
class PartitionInfo:
    number: int
    start: int  # byte offset into the image
    size: int  # byte length
    fstype_hint: str  # parted's guess (may be blank/unreliable — re-detected later)
    name: str  # GPT partition name, if any
    flags: str


def parse_parted_machine(output: str) -> list[PartitionInfo]:
    """Parse ``parted -m ... unit B print`` output into partitions.

    Machine format: a ``BYT;`` header, a device line, then one line per
    partition ``number:start:end:size:fstype:name:flags;`` with byte values
    suffixed ``B``. Malformed lines are skipped.
    """
    parts: list[PartitionInfo] = []
    for raw in output.splitlines():
        line = raw.strip().rstrip(";")
        if not line or line == "BYT" or ":" not in line:
            continue
        fields = line.split(":")
        # Device line's first field is a path (e.g. /dev/sdb or image.img).
        if not fields[0].isdigit():
            continue
        if len(fields) < 7:
            continue
        try:
            number = int(fields[0])
            start = _to_bytes(fields[1])
            size = _to_bytes(fields[3])
        except ValueError:
            continue
        parts.append(
            PartitionInfo(
                number=number,
                start=start,
                size=size,
                fstype_hint=fields[4],
                name=fields[5],
                flags=fields[6],
            )
        )
    return parts


def _to_bytes(value: str) -> int:
    """Convert a parted byte value like ``1048576B`` to an int."""
    v = value.strip()
    if v.endswith("B"):
        v = v[:-1]
    return int(v)


def enumerate_partitions(image_path: str, *, timeout: float | None = 30) -> list[PartitionInfo]:
    """Run parted against ``image_path`` and return its partitions (empty on error)."""
    result = su.run(
        ["parted", "-m", "-s", image_path, "unit", "B", "print"],
        timeout=timeout,
    )
    if not result.ok:
        return []
    return parse_parted_machine(result.stdout)
