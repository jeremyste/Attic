"""HDD capture controllers.

Two phases, because the multi-pass rescue is user-gated (the spec: after each pass
show a summary and let the user choose "another pass" or "accept" — never
auto-decide):

  * ``HddRescueWorker`` runs ONE ddrescue pass against the raw device into a
    persistent staging image + mapfile, then reports the pass summary. Running it
    again resumes from the same mapfile (that's how ddrescue continues).
  * ``HddExtractWorker`` runs after the user accepts: it enumerates partitions
    *from the captured image*, detects/extracts each independently (partitions on
    one drive need not share a filesystem), and builds one catalog row per
    partition (all sharing the drive's source_id + sequence number).

The drive-level image is compressed once by the finalize pool; the per-partition
rows share its filename/size/hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtCore import QThread, pyqtSignal

from ..core import extract as extract_mod
from ..core.catalog import CatalogRow
from ..core.config import EXTRACTED_DIRNAME, FAT_FSTYPES, Status, UNRECOGNIZED_FS_LABEL
from ..core.datescan import scan_tree_date
from ..core.ddrescue import build_ddrescue_argv
from ..core.partition import PartitionInfo, enumerate_partitions
from ..core.sanitize import sanitize_filename
from ..core.staging import StagingDir
from ..core.subprocess_util import with_pkexec
from ..core import subprocess_util as su
from .base import JobRequest, base_row
from .ddrescue_runner import run_ddrescue

# parted's fstype hints normalized to what our extractor/mount expects.
_PARTED_FSTYPE_MAP = {
    "fat12": "fat12", "fat16": "fat16", "fat32": "fat32",
    "ntfs": "ntfs", "ext2": "ext2", "ext3": "ext3", "ext4": "ext4",
}


def normalize_parted_fstype(hint: str) -> str:
    """Map a parted fstype hint to a known fstype, or '' if unusable."""
    return _PARTED_FSTYPE_MAP.get((hint or "").strip().lower(), "")


class HddRescueWorker(QThread):
    """Runs a single ddrescue pass against a device; resumable via the mapfile."""

    map_progress = pyqtSignal(object)  # MapSummary
    stage = pyqtSignal(str)
    log = pyqtSignal(str)
    pass_done = pyqtSignal(object)  # MapSummary (final)
    failed = pyqtSignal(str)

    def __init__(self, device: str, image_path: str, mapfile_path: str,
                 stderr_path: str, *, first_pass: bool, retries: int = 3, parent=None):
        super().__init__(parent)
        self.device = device
        self.image_path = image_path
        self.mapfile_path = mapfile_path
        self.stderr_path = stderr_path
        self.first_pass = first_pass
        self.retries = retries

    def run(self) -> None:
        self.stage.emit("Rescuing drive" if not self.first_pass else "Rescuing drive (first pass)")
        argv = with_pkexec(
            build_ddrescue_argv(
                self.device, self.image_path, self.mapfile_path,
                first_pass_only=self.first_pass, retries=self.retries,
            )
        )
        self.log.emit(" ".join(argv))
        try:
            outcome = run_ddrescue(
                argv, self.mapfile_path, stderr_path=self.stderr_path,
                on_progress=self.map_progress.emit,
                should_cancel=self.isInterruptionRequested,
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        if outcome.returncode != 0 and (
            outcome.last_summary is None or outcome.last_summary.rescued_bytes == 0
        ):
            self.failed.emit(f"ddrescue failed: {outcome.stderr_tail}")
            return
        self.pass_done.emit(outcome.last_summary)


@dataclass
class PartitionOutcome:
    info: PartitionInfo
    folder_name: str  # "Partition 1 - Label" / "Partition 1" / "Extracted Files"
    detected_label: str
    fstype: str
    status: Status
    date_str: str


@dataclass
class HddExtractResult:
    raw_image_path: str
    log_path: str
    rows: list[CatalogRow]  # partial: chosen_name/sequence set by the tab
    fallback_date: str
    overall_status: Status
    partitions: list[PartitionOutcome] = field(default_factory=list)


class HddExtractWorker(QThread):
    """Enumerates + extracts partitions from a captured drive image."""

    stage = pyqtSignal(str)
    log = pyqtSignal(str)
    done = pyqtSignal(object)  # HddExtractResult
    failed = pyqtSignal(str)

    def __init__(self, request: JobRequest, staging: StagingDir,
                 image_path: str, log_path: str, parent=None):
        super().__init__(parent)
        self.request = request
        self.staging = staging
        self.image_path = image_path
        self.log_path = log_path

    def run(self) -> None:
        try:
            self.stage.emit("Enumerating partitions")
            partitions = enumerate_partitions(self.image_path)
            self.log.emit(f"Found {len(partitions)} partition(s)")
            single = len(partitions) == 1

            outcomes: list[PartitionOutcome] = []
            rows: list[CatalogRow] = []
            dates: list[str] = []
            worst = Status.OK

            for idx, part in enumerate(partitions, start=1):
                outcome = self._process_partition(part, idx, single)
                outcomes.append(outcome)
                if outcome.date_str:
                    dates.append(outcome.date_str)
                worst = _worse(worst, outcome.status)
                rows.append(
                    base_row(
                        self.request,
                        sequence_number=0,  # set by tab (drive-level)
                        chosen_name="",     # set by tab (drive-level)
                        detected_label=outcome.detected_label,
                        partition_label=outcome.detected_label,
                        folder_path="",     # set by finalize
                        fallback_date="",   # drive-level; set by tab if fallback used
                        filesystem_detected=outcome.fstype,
                        status=outcome.status,
                    )
                )

            fallback_date = max(dates) if dates else ""
            self.done.emit(
                HddExtractResult(
                    raw_image_path=self.image_path,
                    log_path=self.log_path,
                    rows=rows,
                    fallback_date=fallback_date,
                    overall_status=worst,
                    partitions=outcomes,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _process_partition(self, part: PartitionInfo, idx: int, single: bool) -> PartitionOutcome:
        fstype = normalize_parted_fstype(part.fstype_hint)
        label = self._detect_label(part, fstype)

        if single:
            folder_name = EXTRACTED_DIRNAME
        elif label:
            folder_name = f"Partition {idx} - {sanitize_filename(label)}"
        else:
            folder_name = f"Partition {idx}"

        dest = self.staging.child(folder_name)
        status = Status.OK
        date_str = ""

        if fstype:
            self.stage.emit(f"Extracting partition {idx}")
            result = extract_mod.extract(
                self.image_path, dest, fstype, offset=part.start, size=part.size
            )
            if not result.ok:
                self.log.emit(f"Partition {idx} extraction issue: {result.error_summary}")
                status = Status.PARTIAL
            date_str = scan_tree_date(dest).date_str
        else:
            self.log.emit(f"Partition {idx}: unrecognized filesystem; raw image retained.")
            status = Status.UNRECOGNIZED_FS
            fstype = UNRECOGNIZED_FS_LABEL

        return PartitionOutcome(
            info=part, folder_name=folder_name, detected_label=label,
            fstype=fstype, status=status, date_str=date_str,
        )

    def _detect_label(self, part: PartitionInfo, fstype: str) -> str:
        """Best-effort partition label: mtools for FAT, parted GPT name otherwise."""
        if fstype in FAT_FSTYPES:
            res = su.run(["mlabel", "-i", f"{self.image_path}@@{part.start}", "-s", "::"])
            if res.ok:
                import re
                m = re.search(r"Volume label is ([^\r\n]+?)(?:\s+\(|$)", res.stdout)
                if m:
                    return m.group(1).strip()
        return part.name.strip()


def _worse(a: Status, b: Status) -> Status:
    """Return the more-severe of two statuses for the drive-level rollup."""
    order = {Status.OK: 0, Status.PARTIAL: 1, Status.UNRECOGNIZED_FS: 2, Status.FAILED: 3}
    return a if order[a] >= order[b] else b
