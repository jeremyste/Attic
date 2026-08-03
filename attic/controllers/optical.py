"""Optical (CD/DVD) capture controller.

Home-burned data discs only. ddrescue images the whole optical device (all
sessions, if multisession) into a raw image; the ISO9660/Joliet volume id is used
for label detection; extraction reflects what the disc presents now (standard
mount+copy of the resolved filesystem — multisession "most recent session"
resolution happens naturally, no manual session-picking).
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal

from ..core import extract as extract_mod
from ..core import fsdetect
from ..core.config import EXTRACTED_DIRNAME, Status
from ..core.datescan import scan_tree_date
from ..core.ddrescue import MapSummary, build_ddrescue_argv
from ..core.staging import StagingDir
from ..core.subprocess_util import with_pkexec
from .base import CaptureArtifacts, CaptureWorker
from .ddrescue_runner import run_ddrescue


class OpticalCaptureWorker(CaptureWorker):
    """Images an optical disc, detects its filesystem, and extracts it."""

    # live ddrescue map summary for the rescue-bar widget
    map_progress = pyqtSignal(object)  # MapSummary

    def __init__(self, request, retries: int = 3, parent=None):
        super().__init__(request, parent)
        self.retries = retries

    def capture(self, staging: StagingDir) -> CaptureArtifacts:
        device = self.request.source_id or "/dev/sr0"
        raw = staging.child("disc.img")
        mapfile = staging.child("disc.log")
        stderr_path = staging.child("ddrescue.stderr")

        self.stage.emit("Imaging disc")
        self.log.emit(f"ddrescue {device} -> {raw}")

        argv = with_pkexec(
            build_ddrescue_argv(device, raw, mapfile, optical=True, retries=self.retries)
        )
        outcome = run_ddrescue(
            argv,
            mapfile,
            stderr_path=stderr_path,
            on_progress=self._emit_progress,
            should_cancel=self.isInterruptionRequested,
        )
        # The disc is no longer needed once ddrescue has finished with it;
        # detection, extraction and compression are all host-side work, so the
        # next disc can be loaded while this one finishes processing.
        self.release_drive()

        if outcome.returncode != 0 and (outcome.last_summary is None
                                        or outcome.last_summary.rescued_bytes == 0):
            return CaptureArtifacts(
                raw_image_path=raw, log_path=mapfile,
                status=Status.FAILED,
                error_summary=f"ddrescue failed: {outcome.stderr_tail}",
            )

        status = Status.OK
        if outcome.last_summary and outcome.last_summary.bad_bytes > 0:
            status = Status.PARTIAL

        # Detection: blkid picks up the ISO9660/Joliet volume id + fstype.
        self.stage.emit("Detecting filesystem")
        det = fsdetect.detect_filesystem(raw, mount_probe=extract_mod._mount_probe)

        fallback_date = ""
        date_suspect = False
        if det.recognized:
            self.stage.emit("Extracting files")
            dest = staging.child(EXTRACTED_DIRNAME)
            result = extract_mod.extract(raw, dest, det.fstype)
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
            raw_image_path=raw,
            log_path=mapfile,
            detected_label=det.label,
            fallback_date=fallback_date,
            fallback_date_suspect=date_suspect,
            filesystem_detected=det.fstype,
            status=status,
            error_summary="" if status != Status.FAILED else outcome.stderr_tail,
        )

    def _emit_progress(self, summary: MapSummary) -> None:
        self.map_progress.emit(summary)
        if summary.total_bytes:
            self.progress.emit(int(summary.rescued_fraction * 100))
