"""ddrescue command assembly + mapfile parsing (pure, testable).

Both the HDD and Optical pipelines drive ``ddrescue`` and visualize its progress
by polling the mapfile it maintains. The *running* of ddrescue is a subprocess
concern handled by the controllers; the argv construction and mapfile parsing
here are pure so they can be unit-tested without a device.

We follow the standard multi-pass strategy (fast first pass, then retries with
``-r``); exact flags are conservative and cross-version-safe. Callers should still
consult the installed ``ddrescue --help`` for anything version-specific.
"""

from __future__ import annotations

from dataclasses import dataclass

# ddrescue mapfile status characters and a human meaning for each.
#   '?' non-tried    '*' non-trimmed    '/' non-scraped
#   '-' bad-sector   '+' finished(rescued)
STATUS_MEANING = {
    "?": "non-tried",
    "*": "non-trimmed",
    "/": "non-scraped",
    "-": "bad-sector",
    "+": "rescued",
}


@dataclass
class MapSegment:
    pos: int  # byte offset
    size: int  # byte length
    status: str  # one of STATUS_MEANING keys


@dataclass
class MapSummary:
    """Aggregate byte counts per status, plus totals, from a mapfile."""

    segments: list[MapSegment]
    by_status: dict[str, int]
    total_bytes: int
    current_pos: int = 0
    current_status: str = ""

    @property
    def rescued_bytes(self) -> int:
        return self.by_status.get("+", 0)

    @property
    def bad_bytes(self) -> int:
        return self.by_status.get("-", 0)

    @property
    def nontried_bytes(self) -> int:
        return self.by_status.get("?", 0)

    @property
    def rescued_fraction(self) -> float:
        return self.rescued_bytes / self.total_bytes if self.total_bytes else 0.0


# ddrescue's own four-phase algorithm (see `info ddrescue` -> Algorithm):
# copying (up to 5 sub-passes, not user-limitable) -> trimming (1 pass,
# delimits bad blocks' edges) -> scraping (1 pass, sector-by-sector sweep of
# what's left) -> retrying (up to --retry-passes, alternating direction).
# Only "full" runs all four; each earlier value stops before the named phase.
DDRESCUE_STOP_AFTER_CHOICES = ("copying", "trimming", "scraping", "full")


def build_ddrescue_argv(
    device: str,
    image_path: str,
    mapfile_path: str,
    *,
    optical: bool = False,
    retries: int = 3,
    first_pass_only: bool = False,
    timeout_minutes: int = 0,
    stop_after: str = "full",
) -> list[str]:
    """Assemble a ddrescue invocation (to be wrapped with pkexec by the caller).

    ``optical`` sets a 2048-byte sector size and idirect read, appropriate for
    CD/DVD. ``first_pass_only`` skips the retry/scrape phases (``-n``) for a fast
    initial pass whose summary the user then reviews -- equivalent to
    ``stop_after="trimming"``, just named for that specific HDD workflow step.
    ``timeout_minutes`` > 0 adds ``-T <n>m``, ddrescue's own "give up" clock --
    it measures time since the *last successful read*, not total run time, so a
    disk that is mostly readable never trips it; only a stretch of genuinely
    stuck retries does.

    ``stop_after`` is the "how many of ddrescue's phases should this job go
    through" knob (see :data:`DDRESCUE_STOP_AFTER_CHOICES`): "copying" skips
    trimming/scraping/retrying entirely (``-N -n``, fastest, keeps only the
    easily-read majority), "trimming" additionally does the edge-delimiting
    pass but skips scraping/retrying (``-n``), "scraping" runs everything but
    the retry passes (``--retry-passes`` omitted), and "full" (the default,
    today's behavior) also retries bad sectors up to ``retries`` times.
    ``retries`` itself only matters when ``stop_after == "full"``.
    """
    argv = ["ddrescue"]
    if optical:
        argv += ["-b", "2048", "-d"]  # 2048-byte blocks, direct access

    no_trim = stop_after == "copying"
    no_scrape = first_pass_only or stop_after in ("copying", "trimming")
    no_retry = first_pass_only or stop_after in ("copying", "trimming", "scraping")

    if no_trim:
        argv += ["-N"]
    if no_scrape:
        argv += ["-n"]
    if not no_retry:
        argv += [f"-r{retries}"]  # retry bad areas N times

    if timeout_minutes > 0:
        argv += ["-T", f"{timeout_minutes}m"]
    argv += [device, image_path, mapfile_path]
    return argv


def parse_mapfile(text: str) -> MapSummary:
    """Parse GNU ddrescue mapfile text into a :class:`MapSummary`.

    Format: comment lines start with ``#``; the first non-comment line is the
    status line ``<current_pos> <current_status> <current_pass>``; subsequent
    lines are ``<pos> <size> <status>`` block records (hex offsets/sizes).
    Robust to blank lines and partial/short reads.
    """
    segments: list[MapSegment] = []
    by_status: dict[str, int] = {}
    current_pos = 0
    current_status = ""
    seen_status_line = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not seen_status_line:
            # Status line: current_pos current_status [pass]
            seen_status_line = True
            try:
                current_pos = int(parts[0], 16)
                current_status = parts[1] if len(parts) > 1 else ""
            except (ValueError, IndexError):
                pass
            continue
        # Block record: pos size status
        if len(parts) < 3:
            continue
        try:
            pos = int(parts[0], 16)
            size = int(parts[1], 16)
        except ValueError:
            continue
        status = parts[2]
        segments.append(MapSegment(pos=pos, size=size, status=status))
        by_status[status] = by_status.get(status, 0) + size

    total = sum(by_status.values())
    return MapSummary(
        segments=segments,
        by_status=by_status,
        total_bytes=total,
        current_pos=current_pos,
        current_status=current_status,
    )
