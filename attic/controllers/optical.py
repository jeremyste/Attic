"""Optical (CD/DVD) capture controller.

Home-burned data discs only. ddrescue images the whole optical device (all
sessions, if multisession) into a raw image; the ISO9660/Joliet volume id is used
for label detection; extraction reflects what the disc presents now (standard
mount+copy of the resolved filesystem — multisession "most recent session"
resolution happens naturally, no manual session-picking).
"""

from __future__ import annotations

import os

from PyQt6.QtCore import pyqtSignal

from ..core import dvdvideo
from ..core import extract as extract_mod
from ..core import fsdetect
from ..core import subprocess_util as su
from ..core.config import EXTRACTED_DIRNAME, Status
from ..core.datescan import scan_tree_date
from ..core.ddrescue import MapSummary, build_ddrescue_argv
from ..core.staging import StagingDir
from .base import CaptureArtifacts, CaptureWorker
from .ddrescue_runner import run_ddrescue


class OpticalCaptureWorker(CaptureWorker):
    """Images an optical disc, detects its filesystem, and extracts it."""

    # live ddrescue map summary for the rescue-bar widget
    map_progress = pyqtSignal(object)  # MapSummary

    def __init__(
        self, request, retries: int = 3, *,
        convert_dvd_video: bool = True, dvd_video_crf: int = 18,
        eject_on_complete: bool = True,
        parent=None,
    ):
        super().__init__(request, parent)
        self.retries = retries
        self.convert_dvd_video = convert_dvd_video
        self.dvd_video_crf = dvd_video_crf
        self.eject_on_complete = eject_on_complete

    def capture(self, staging: StagingDir) -> CaptureArtifacts:
        device = self.request.source_id or "/dev/sr0"
        raw = staging.child("disc.img")
        mapfile = staging.child("disc.log")
        stderr_path = staging.child("ddrescue.stderr")

        self.stage.emit("Imaging disc")
        self.log.emit(f"ddrescue {device} -> {raw}")

        argv = build_ddrescue_argv(device, raw, mapfile, optical=True, retries=self.retries)
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
        if self.eject_on_complete:
            self._eject(device)

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
        notes = ""
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

            if self.convert_dvd_video:
                notes, video_degraded = self._convert_dvd_video(dest, det.label)
                if video_degraded and status == Status.OK:
                    status = Status.PARTIAL
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
            notes=notes,
        )

    def _convert_dvd_video(
        self, extracted_dir: str, detected_label: str,
    ) -> tuple[str, bool]:
        """If ``extracted_dir`` is a DVD-Video (VIDEO_TS) disc, transcode its
        title(s) into ordinary .mp4 files alongside the raw VIDEO_TS copy.

        Returns ``(note, degraded)``: ``note`` is "" if this wasn't a
        DVD-Video disc at all, else a summary of what happened; ``degraded``
        is True if it *was* a DVD-Video disc but conversion didn't fully
        succeed (worth reflecting in the overall capture status). Never
        raises -- a transcode problem is logged and noted, not a capture
        failure; the raw VIDEO_TS copy from normal extraction is always kept
        regardless.
        """
        base_name = (detected_label or "Video").strip() or "Video"
        video_dest = os.path.join(extracted_dir, "video")
        self.stage.emit("Checking for DVD video")
        if dvdvideo.find_video_ts_dir(extracted_dir) is None:
            return "", False
        self.stage.emit("Converting DVD video (this can take a while)")
        result = dvdvideo.convert(
            extracted_dir, video_dest, base_name, crf=self.dvd_video_crf,
        )
        if result is None:
            return "", False
        if not result.titles:
            self.log.emit(f"DVD video: {result.error_summary}")
            return result.error_summary, True
        for t in result.titles:
            outcome = "ok" if t.ok else f"FAILED: {t.error_summary}"
            self.log.emit(f"DVD video title {t.title.number}: {outcome}")
        if result.ok:
            note = (
                f"DVD-Video: converted {result.converted_count}/{len(result.titles)} "
                f"title(s) to Extracted Files/video/"
                + (f" -- {result.error_summary}" if result.error_summary else "")
            )
            return note, result.converted_count < len(result.titles)
        return f"DVD-Video: conversion failed -- {result.error_summary}", True

    def _eject(self, device: str) -> None:
        """Best-effort tray eject once the disc is safely imaged.

        A physical cue that it's safe to pull the disc and load the next one
        -- never lets an eject problem (no `eject` binary, permissions,
        a slot-load drive that doesn't support it) affect the capture result.
        """
        result = su.run(["eject", device])
        if not result.ok:
            self.log.emit(f"Eject failed (non-fatal): {result.error_summary()}")

    def _emit_progress(self, summary: MapSummary) -> None:
        self.map_progress.emit(summary)
        if summary.total_bytes:
            self.progress.emit(int(summary.rescued_fraction * 100))
