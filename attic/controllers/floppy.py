"""Floppy capture controller (Greaseweazle).

``gw read`` captures flux and decodes it to a sector image. gw prints one line
per track as it goes; we stream that output, parse each track's result, and emit
a per-track signal so the cylinder×head grid widget can update live. After the
read we run the shared filesystem-detection chain (don't assume FAT12 — these
disks span DOS through Windows XP era) and extract if recognized.

gw flag syntax varies by version; ``gw read <image>`` is the stable core. Callers
should verify the installed gw's ``--help`` for anything version-specific.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from PyQt6.QtCore import pyqtSignal

from ..core import extract as extract_mod
from ..core import fsdetect
from ..core.config import EXTRACTED_DIRNAME, Status
from ..core.datescan import scan_tree_date
from ..core.staging import StagingDir
from .base import CaptureArtifacts, CaptureWorker

# Track read outcomes for the grid colour coding.
TRACK_CLEAN = "clean"
TRACK_RETRIED = "retried"
TRACK_FAILED = "failed"

# e.g. "T0.0: IBM MFM (18/18 sectors)"  /  "T12.1: ... (17/18 sectors)"
_TRACK_RE = re.compile(
    r"T(?P<cyl>\d+)\.(?P<head>\d+):.*?\((?P<got>\d+)/(?P<total>\d+)\s+sectors?\)",
    re.IGNORECASE,
)
_RETRY_RE = re.compile(r"retry|retrying", re.IGNORECASE)


@dataclass
class TrackResult:
    cyl: int
    head: int
    status: str  # TRACK_CLEAN / TRACK_RETRIED / TRACK_FAILED
    sectors_got: int = 0
    sectors_total: int = 0


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

    def capture(self, staging: StagingDir) -> CaptureArtifacts:
        image = staging.child("floppy.img")
        log_path = staging.child("floppy.log")

        self.stage.emit("Reading floppy")
        self.log.emit(f"gw read -> {image}")

        any_track = False
        failed_tracks = 0
        with open(log_path, "w", encoding="utf-8", errors="replace") as logfh:
            proc = subprocess.Popen(
                ["gw", "read", image],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                logfh.write(line)
                self.log.emit(line.rstrip())
                tr = parse_gw_track_line(line)
                if tr:
                    any_track = True
                    if tr.status == TRACK_FAILED:
                        failed_tracks += 1
                    self.track_read.emit(tr)
                if self.isInterruptionRequested():
                    proc.terminate()
                    break
            proc.wait()

        if proc.returncode != 0 and not any_track:
            return CaptureArtifacts(
                raw_image_path=image, log_path=log_path, status=Status.FAILED,
                error_summary="gw read failed (see log)",
            )

        status = Status.PARTIAL if failed_tracks else Status.OK

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
            log_path=log_path,
            detected_label=det.label,
            fallback_date=fallback_date,
            fallback_date_suspect=date_suspect,
            filesystem_detected=det.fstype,
            status=status,
        )
