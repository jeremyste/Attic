"""Floppy capture controller (Greaseweazle).

``gw read`` captures flux and decodes it to a sector image. gw prints one line
per track as it goes; we stream that output, parse each track's result, and emit
a per-track signal so the cylinder×head grid widget can update live. After the
read we run the shared filesystem-detection chain (don't assume FAT12 — these
disks span DOS through Windows XP era) and extract if recognized.

Two things about gw are load-bearing here and were verified against a real
Greaseweazle V4.1 (host tools 1.23, firmware 1.6):

* Writing a *sector* image (.img) is refused outright unless ``--format`` is
  given ("Sector image requires a disk format to be specified"), so the format
  is always passed. Only a raw flux image (.scp/.raw) may omit it.
* ``gw read`` swallows device-level failures: it catches ``USB.CmdError``,
  prints ``Command Failed: ...``, and still **exits 0** while writing no image
  at all (e.g. no disk in the drive -> "GetFluxStatus: No Index"). The exit
  status alone therefore cannot be trusted; we also scan the output for gw's
  error markers and require a non-empty image on disk.
"""

from __future__ import annotations

import collections
import os
import re
import subprocess
from dataclasses import dataclass, field

from PyQt6.QtCore import pyqtSignal

from ..core import extract as extract_mod
from ..core import fsdetect
from ..core.config import EXTRACTED_DIRNAME, UNRECOGNIZED_FS_LABEL, Status
from ..core.datescan import scan_tree_date
from ..core.staging import StagingDir
from .base import CaptureArtifacts, CaptureWorker

# Track read outcomes for the grid colour coding.
TRACK_CLEAN = "clean"
TRACK_RETRIED = "retried"
TRACK_FAILED = "failed"

# e.g. "T0.0: IBM MFM (18/18 sectors) from 250kbps, 300rpm"
# When the logical track differs from the physical one (step=/hswap/head offsets)
# gw inserts a remap before the colon: "T40.0 <- Drive 20.0: IBM MFM (9/9 ...)".
# The cyl/head we want is the logical one, i.e. the first pair.
_TRACK_RE = re.compile(
    r"T(?P<cyl>\d+)\.(?P<head>\d+)(?:\s*<-[^:]*)?:"
    r".*?\((?P<got>\d+)/(?P<total>\d+)\s+sectors?\)",
    re.IGNORECASE,
)
_RETRY_RE = re.compile(r"retry|retrying", re.IGNORECASE)

# gw's two error markers. "** FATAL ERROR:" is printed by the CLI wrapper with
# the message on the following line(s); "Command Failed:" carries its message
# inline and, unlike the fatal path, still exits 0.
_FATAL_MARKER = "** FATAL ERROR:"
_CMD_FAILED_MARKER = "Command Failed:"


@dataclass
class TrackResult:
    cyl: int
    head: int
    status: str  # TRACK_CLEAN / TRACK_RETRIED / TRACK_FAILED
    sectors_got: int = 0
    sectors_total: int = 0


@dataclass
class _GwRun:
    """Outcome of one streamed gw invocation."""

    # Keyed by (cyl, head): gw prints a line per retry of the same track, so
    # counting lines would multiply-count one bad track.
    tracks: dict[tuple[int, int], str] = field(default_factory=dict)
    # Same keying, but the full TrackResult (so sectors_total survives for
    # geometry-consistency checks -- see infer_uniform_format).
    track_results: dict[tuple[int, int], TrackResult] = field(default_factory=dict)
    error: str = ""  # first hard error gw reported, if any
    returncode: int = 0
    cancelled: bool = False

    @property
    def failed_tracks(self) -> int:
        return sum(1 for s in self.tracks.values() if s == TRACK_FAILED)


# ibm.scan probes each track's sector count independently. A FAT/DOS volume's
# boot sector always declares one *uniform* sectors-per-track value and every
# reader (mtools, the kernel, ...) trusts that uniformly. When scan
# mis-detects even a single track (seen in the wild: the first cylinder read
# one sector short), the flat .img gw assembles from those per-track counts
# no longer matches the geometry the filesystem itself expects -- every byte
# of file data past the short track lands earlier than the FAT says it
# should, silently corrupting the whole disk's files even though every
# sector's own CRC validated cleanly. Mapping the majority per-track count to
# a fixed, uniform gw format sidesteps this. Each entry is the 2-head/80-cyl
# canonical variant (matching what ibm.scan itself always scans), since a
# smaller real disk just contributes trailing empty tracks either way.
_STANDARD_SECS_TO_FORMAT = {
    8: "ibm.320",
    9: "ibm.720",
    10: "ibm.800",
    15: "ibm.1200",
    18: "ibm.1440",
    21: "ibm.dmf",
    36: "ibm.2880",
}


def infer_uniform_format(
    track_results: dict[tuple[int, int], TrackResult], requested_format: str,
) -> str | None:
    """A fixed gw format to use instead of ``requested_format``, or None.

    Only fires when ``requested_format`` is the auto-scanning ``ibm.scan``
    profile and the read found a clear majority sectors-per-track count with
    at least one track disagreeing -- i.e. there is both a correction to make
    and enough data to be confident what it should be. A 3:1 majority is
    required before trusting the vote; anything murkier (a genuinely mixed or
    exotic disk) is left alone rather than guessing.
    """
    if requested_format.strip().casefold() != "ibm.scan":
        return None
    totals = [tr.sectors_total for tr in track_results.values() if tr.sectors_total > 0]
    if len(totals) < 2 or len(set(totals)) == 1:
        return None  # nothing to reconcile, or too little data to trust a vote
    counts = collections.Counter(totals)
    majority_total, majority_n = counts.most_common(1)[0]
    if majority_n < 0.75 * len(totals):
        return None  # no clear majority -- don't guess on a mixed/exotic disk
    return _STANDARD_SECS_TO_FORMAT.get(majority_total)


def _nonempty(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def build_gw_read_cmd(
    out_path: str, *, disk_format: str = "ibm.scan", device: str = "",
    raw: bool = False, revs: int = 0, retries: int = 0, seek_retries: int = 0,
) -> list[str]:
    """Build the ``gw read`` argv.

    ``disk_format`` is mandatory for a sector image; an empty value would make
    gw abort before touching the drive, so we fall back to the scanning format
    rather than emit a command that cannot work. ``device`` is omitted when
    blank so gw auto-detects the port.

    ``raw`` writes the preserved flux stream. It is required for a real flux
    master: writing a .scp *without* it makes gw re-synthesize flux from the
    decoded sectors, which throws away exactly the information the flux copy
    exists to keep. ``--format`` is still passed alongside ``--raw`` (where it
    "verifies only"), because that is what makes gw decode and report each
    track's sector counts as it goes -- without it the per-track output carries
    no sector totals and the live track grid has nothing to show.

    ``revs`` of 0 leaves gw on the format's own default (2 for ibm.scan).

    ``retries`` (in-place re-reads of the same track) and ``seek_retries``
    (retract the head and re-seek before retrying -- can dislodge dust/debris
    or land the head slightly differently than an in-place retry) both leave
    gw on its own defaults (3 and 0) at 0. Verified against real damaged
    media: a genuinely bad sector reads back byte-for-byte, flux-sample-for-
    -flux-sample identical on every attempt regardless of retry count, so
    these only ever help a *marginal* read (dust, borderline signal) -- but
    since gw stops retrying the moment a track fully succeeds, raising both
    costs virtually nothing on the healthy majority of a disk while giving a
    damaged track more chances, which is the right trade for one-shot
    archival media.
    """
    cmd = ["gw", "read", "--format", disk_format.strip() or "ibm.scan"]
    if raw:
        cmd.append("--raw")
    if revs > 0:
        cmd += ["--revs", str(revs)]
    if retries > 0:
        cmd += ["--retries", str(retries)]
    if seek_retries > 0:
        cmd += ["--seek-retries", str(seek_retries)]
    if device.strip():
        cmd += ["--device", device.strip()]
    cmd.append(out_path)
    return cmd


def build_gw_convert_cmd(
    flux_path: str, image: str, *, disk_format: str = "ibm.scan"
) -> list[str]:
    """Build the ``gw convert`` argv that decodes a flux stream to a sector image.

    Pure host-side work: the drive is already free by the time this runs.
    """
    return [
        "gw", "convert", "--format", disk_format.strip() or "ibm.scan",
        flux_path, image,
    ]


def _parse_set(spec: str) -> int:
    """Count members of a gw SET: comma-separated integers and ranges."""
    total = 0
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            total += int(hi) - int(lo) + 1
        else:
            total += 1
    return total


def parse_track_total(line: str) -> int | None:
    """Total tracks a gw stage will touch, from its header line.

    gw announces the work up front -- "Reading c=0-79:h=0-1 revs=2" or
    "Converting c=0-79:h=0-1 -> ..." -- which is the only honest source of a
    denominator for a progress bar. Returns None for any other line.
    """
    m = re.match(r"\s*(?:Reading|Converting)\s+(c=[^\s]+)", line)
    if not m:
        return None
    cyls = heads = 0
    for field_spec in m.group(1).split(":"):
        key, _, value = field_spec.partition("=")
        if key == "c":
            cyls = _parse_set(value)
        elif key == "h":
            heads = _parse_set(value)
    if cyls <= 0:
        return None
    return cyls * max(heads, 1)


def parse_gw_track_line(line: str) -> TrackResult | None:
    """Parse one gw read output line into a :class:`TrackResult` (or None).

    Pure and unit-tested. A track is CLEAN when all sectors were read, FAILED
    when none were, RETRIED otherwise or when the line mentions a retry.
    """
    m = _TRACK_RE.search(line)
    if not m:
        return None
    got = int(m.group("got"))
    total = int(m.group("total"))
    if got == 0:
        status = TRACK_FAILED
    elif got < total or _RETRY_RE.search(line):
        status = TRACK_RETRIED
    else:
        status = TRACK_CLEAN
    return TrackResult(
        cyl=int(m.group("cyl")), head=int(m.group("head")),
        status=status, sectors_got=got, sectors_total=total,
    )


class FloppyCaptureWorker(CaptureWorker):
    """Reads a floppy with gw, then detects/extracts its filesystem."""

    track_read = pyqtSignal(object)  # TrackResult

    def __init__(self, request, parent=None, *, disk_format: str = "ibm.scan",
                 device: str = "", capture_flux: bool = True, flux_revs: int = 0,
                 retries: int = 0, seek_retries: int = 0):
        super().__init__(request, parent)
        self.disk_format = disk_format
        self.device = device
        self.capture_flux = capture_flux
        self.flux_revs = flux_revs
        self.retries = retries
        self.seek_retries = seek_retries

    # --- gw process plumbing -------------------------------------------------

    def _run_gw(self, cmd: list[str], logfh, *, watch_tracks: bool) -> _GwRun:
        """Run one gw command, streaming its output to the log and the UI.

        Returns what the run produced: per-track outcomes (when
        ``watch_tracks``), the first hard error gw reported, and whether the
        user interrupted it.
        """
        self.log.emit(" ".join(cmd))
        run = _GwRun()
        awaiting_fatal_text = False
        total_tracks = 0

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            logfh.write(line)
            self.log.emit(line.rstrip())

            stripped = line.strip()
            if awaiting_fatal_text:
                # The CLI prints the marker, then the dedented message body.
                if stripped:
                    run.error = run.error or stripped
                    awaiting_fatal_text = False
            elif stripped.startswith(_FATAL_MARKER):
                awaiting_fatal_text = True
            elif stripped.startswith(_CMD_FAILED_MARKER) and not run.error:
                run.error = stripped

            # Both the read and the decode announce their track count up front,
            # which is what makes a real (rather than invented) progress bar
            # possible for each stage.
            if not total_tracks:
                total_tracks = parse_track_total(line) or 0

            tr = parse_gw_track_line(line)
            if tr:
                if watch_tracks:
                    run.tracks[(tr.cyl, tr.head)] = tr.status
                    self.track_read.emit(tr)
                else:
                    run.tracks.setdefault((tr.cyl, tr.head), tr.status)
                # Always overwritten (not setdefault): a retried track reprints
                # the same line, and the last one is the final/best result.
                run.track_results[(tr.cyl, tr.head)] = tr
                if total_tracks:
                    self.progress.emit(
                        min(100, round(100 * len(run.tracks) / total_tracks))
                    )
            if self.isInterruptionRequested():
                run.cancelled = True
                proc.terminate()
                break
        proc.wait()
        run.returncode = proc.returncode
        return run

    # --- capture modes -------------------------------------------------------

    def capture(self, staging: StagingDir) -> CaptureArtifacts:
        log_path = staging.child("floppy.log")
        with open(log_path, "w", encoding="utf-8", errors="replace") as logfh:
            if self.capture_flux:
                return self._capture_via_flux(staging, log_path, logfh)
            return self._capture_direct(staging, log_path, logfh)

    def _capture_via_flux(
        self, staging: StagingDir, log_path: str, logfh
    ) -> CaptureArtifacts:
        """Preserve flux first, then decode it to a sector image host-side.

        The drive is only needed for step one, so the disk can be swapped as
        soon as the read finishes; decode and extraction are pure host work.
        """
        flux = staging.child("floppy.scp")
        image = staging.child("floppy.img")

        self.stage.emit("Reading floppy (flux)")
        read = self._run_gw(
            build_gw_read_cmd(
                flux, disk_format=self.disk_format, device=self.device,
                raw=True, revs=self.flux_revs,
                retries=self.retries, seek_retries=self.seek_retries,
            ),
            logfh, watch_tracks=True,
        )

        # The drive is done the moment the flux is on disk; everything below is
        # pure host work, so let the next disk be loaded now.
        self.release_drive()

        if read.cancelled:
            return CaptureArtifacts(
                raw_image_path="", flux_path=flux, log_path=log_path,
                status=Status.FAILED,
                error_summary="Cancelled before the read finished",
            )
        if not _nonempty(flux):
            return CaptureArtifacts(
                raw_image_path="", flux_path="", log_path=log_path,
                status=Status.FAILED,
                error_summary=read.error or "gw read failed (see log)",
            )

        status = Status.PARTIAL if read.failed_tracks else Status.OK
        if read.error or read.returncode != 0:
            self.log.emit(f"gw reported an error; flux kept as partial: {read.error}")
            status = Status.PARTIAL

        # Decode host-side. A flux stream that yields no image is still worth
        # archiving -- that is precisely the case the flux master exists for.
        convert_format = self.disk_format
        override = infer_uniform_format(read.track_results, self.disk_format)
        if override:
            self.log.emit(
                f"{self.disk_format} reported inconsistent sectors/track across "
                f"the disk; decoding with {override} instead for a "
                f"geometry-consistent image (the flux master is unaffected -- "
                f"this only changes how the .img gets assembled from it)."
            )
            convert_format = override
        self.stage.emit("Decoding flux to sector image")
        convert = self._run_gw(
            build_gw_convert_cmd(flux, image, disk_format=convert_format),
            logfh, watch_tracks=False,
        )
        if not _nonempty(image):
            self.log.emit(
                "Flux did not decode to a sector image; archiving the flux alone."
            )
            return CaptureArtifacts(
                raw_image_path="", flux_path=flux, log_path=log_path,
                filesystem_detected=UNRECOGNIZED_FS_LABEL,
                status=Status.UNRECOGNIZED_FS,
                error_summary=convert.error,
            )

        return self._detect_and_extract(
            staging, image, log_path, status, flux_path=flux
        )

    def _capture_direct(
        self, staging: StagingDir, log_path: str, logfh
    ) -> CaptureArtifacts:
        """Read straight to a sector image, keeping no flux."""
        image = staging.child("floppy.img")

        self.stage.emit("Reading floppy")
        read = self._run_gw(
            build_gw_read_cmd(
                image, disk_format=self.disk_format, device=self.device,
                retries=self.retries, seek_retries=self.seek_retries,
            ),
            logfh, watch_tracks=True,
        )

        # Drive free from here on; detection and extraction are host-side.
        self.release_drive()

        # gw writes the image only once the read completes, so a missing or
        # empty file means nothing usable was captured -- regardless of exit code.
        if read.cancelled:
            return CaptureArtifacts(
                raw_image_path=image, log_path=log_path, status=Status.FAILED,
                error_summary="Cancelled before the read finished",
            )
        if not _nonempty(image) or (read.error and not read.tracks):
            return CaptureArtifacts(
                raw_image_path=image, log_path=log_path, status=Status.FAILED,
                error_summary=read.error or "gw read failed (see log)",
            )

        status = Status.PARTIAL if read.failed_tracks else Status.OK
        if read.error or read.returncode != 0:
            # An image exists but gw also reported trouble: keep what we got and
            # flag it rather than presenting a truncated read as a clean one.
            self.log.emit(f"gw reported an error; image kept as partial: {read.error}")
            status = Status.PARTIAL

        if infer_uniform_format(read.track_results, self.disk_format):
            # Same inconsistent-geometry hazard as the flux path (see
            # infer_uniform_format), but this mode keeps no flux to re-decode
            # from -- there is no image left to fix, only to flag as suspect.
            self.log.emit(
                f"{self.disk_format} reported inconsistent sectors/track across "
                f"the disk; this direct-read mode has no preserved flux to "
                f"re-decode from, so the image may be geometry-inconsistent "
                f"(files could be silently misaligned despite good sector CRCs). "
                f"Use flux-preserving capture to avoid this."
            )
            status = Status.PARTIAL

        return self._detect_and_extract(staging, image, log_path, status)

    def _detect_and_extract(
        self, staging: StagingDir, image: str, log_path: str,
        status: Status, *, flux_path: str = "",
    ) -> CaptureArtifacts:
        self.stage.emit("Detecting filesystem")
        det = fsdetect.detect_filesystem(image, mount_probe=extract_mod._mount_probe)

        fallback_date = ""
        date_suspect = False
        if det.recognized:
            self.stage.emit("Extracting files")
            dest = staging.child(EXTRACTED_DIRNAME)
            result = extract_mod.extract(image, dest, det.fstype)
            if not result.ok:
                self.log.emit(f"Extraction issue: {result.error_summary}")
                status = Status.PARTIAL if status == Status.OK else status
            scan = scan_tree_date(dest)
            fallback_date = scan.date_str
            date_suspect = scan.suspect
        else:
            self.log.emit("Filesystem not recognized; keeping raw image only.")
            status = Status.UNRECOGNIZED_FS

        return CaptureArtifacts(
            raw_image_path=image,
            flux_path=flux_path,
            log_path=log_path,
            detected_label=det.label,
            fallback_date=fallback_date,
            fallback_date_suspect=date_suspect,
            filesystem_detected=det.fstype,
            status=status,
        )
