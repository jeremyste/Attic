"""Background finalize pool: compression, checksums, promotion, catalog append.

Decoupled from capture (the spec concurrency model): once a capture worker has a
raw image staged, the expensive ``zstd`` pass + hashing is handed here so it runs
on a shared ``QThreadPool`` without blocking the next capture on any pipeline.

A finalize task, on success:
  1. compresses the staged raw image (zstd -19 --long -T0) and hashes both files,
  2. deletes the now-redundant raw ``.img`` (only the compressed image is kept),
  3. copies the webcam photo into the staging dir (if one was taken),
  4. fills image filename/size/hash fields into every catalog row,
  5. atomically promotes the staging dir into its final Floppy/HDD/CD location,
  6. appends the row(s) to the catalog.

On failure it leaves the staging dir in place and appends a failed row whose
notes point at that staging path, so nothing is silently lost.
"""

from __future__ import annotations

import os
import shutil
import traceback
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from ..core import catalog, compression, staging
from ..core.catalog import CatalogRow
from ..core.config import MediaType, Status
from ..core.staging import StagingDir


@dataclass
class FinalizeRequest:
    """Inputs to one finalize job."""

    working_folder: str
    media_type: MediaType
    staging: StagingDir
    raw_image_path: str  # inside staging (any working name)
    chosen_name: str  # final top-level folder name for this item/drive
    rows: list[CatalogRow]  # one (single volume) or many (HDD partitions)
    log_path: str = ""  # staged ddrescue/gw logfile to rename to {chosen}.log
    # webcam photos to copy in, mapped {filename_suffix: temp_path}
    photos: dict[str, str] = field(default_factory=dict)
    keep_raw: bool = False  # normally False — only the compressed image is kept
    zstd_level: int = 19
    zstd_long: bool = True


class _Signals(QObject):
    progress = pyqtSignal(str, str)  # (chosen_name, stage text)
    done = pyqtSignal(str, object)  # (final_dir, rows)
    failed = pyqtSignal(str, str)  # (chosen_name, error summary)


class _FinalizeTask(QRunnable):
    def __init__(self, req: FinalizeRequest, signals: _Signals):
        super().__init__()
        self.req = req
        self.signals = signals

    def run(self) -> None:  # QRunnable entry point (pool thread)
        req = self.req
        try:
            # Rename staged artifacts to the resolved {chosen_name} before work.
            raw_path = _rename_to(
                req.raw_image_path, req.staging.child(f"{req.chosen_name}.img")
            )
            if req.log_path and os.path.exists(req.log_path):
                _rename_to(req.log_path, req.staging.child(f"{req.chosen_name}.log"))

            out_path = req.staging.child(f"{req.chosen_name}.img.zst")
            self.signals.progress.emit(req.chosen_name, "Compressing")
            result = compression.compress_and_checksum(
                raw_path, out_path, level=req.zstd_level, long_mode=req.zstd_long,
            )

            if not req.keep_raw:
                _remove_quiet(raw_path)

            for suffix, src in req.photos.items():
                if src and os.path.exists(src):
                    shutil.copy2(src, req.staging.child(f"{req.chosen_name}{suffix}"))

            final_dir = staging.final_dir(
                req.working_folder, req.media_type, req.chosen_name
            )
            rel_folder = os.path.relpath(final_dir, req.working_folder)

            comp_name = os.path.basename(result.compressed_path)
            raw_name = os.path.basename(raw_path)
            for row in req.rows:
                row.folder_path = rel_folder
                row.raw_image_filename = "" if not req.keep_raw else raw_name
                row.compressed_image_filename = comp_name
                row.raw_size_bytes = str(result.raw_size_bytes)
                row.compressed_size_bytes = str(result.compressed_size_bytes)
                row.sha256_raw = result.sha256_raw
                row.sha256_compressed = result.sha256_compressed

            self.signals.progress.emit(req.chosen_name, "Finalizing")
            staging.promote(req.staging, final_dir)
            catalog.append_rows(req.working_folder, req.rows)

            self.signals.done.emit(final_dir, req.rows)
        except Exception as exc:  # noqa: BLE001 - never crash the pool thread
            summary = f"{type(exc).__name__}: {exc}"
            self._record_failure(summary, traceback.format_exc())
            self.signals.failed.emit(req.chosen_name, summary)

    def _record_failure(self, summary: str, tb: str) -> None:
        """Append a failed catalog row pointing at the surviving staging dir."""
        req = self.req
        note = f"Temp dir left for inspection: {req.staging.rel_path()}"
        try:
            for row in req.rows:
                row.status = Status.FAILED.value
                row.error_summary = summary
                row.notes = (row.notes + " | " + note).strip(" |")
            catalog.append_rows(req.working_folder, req.rows or [
                CatalogRow(
                    media_type=req.media_type.value,
                    chosen_name=req.chosen_name,
                    status=Status.FAILED.value,
                    error_summary=summary,
                    notes=note,
                )
            ])
        except Exception:  # noqa: BLE001 - best effort; failure already surfaced
            pass


@dataclass
class FinalizePool:
    """Thin wrapper around a shared QThreadPool for finalize jobs.

    Signals are exposed on :attr:`signals` for the UI to connect to.
    """

    max_threads: int = 0  # 0 -> Qt default (cpu count)
    signals: _Signals = field(default_factory=_Signals)
    _pool: QThreadPool = field(default=None, repr=False)

    def __post_init__(self):
        self._pool = QThreadPool.globalInstance()
        if self.max_threads:
            self._pool.setMaxThreadCount(self.max_threads)

    def submit(self, request: FinalizeRequest) -> None:
        self._pool.start(_FinalizeTask(request, self.signals))

    def wait(self, msecs: int = -1) -> bool:
        return self._pool.waitForDone(msecs)


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _rename_to(src: str, dst: str) -> str:
    """Rename ``src`` to ``dst`` within the same staging dir; no-op if equal."""
    if os.path.abspath(src) != os.path.abspath(dst):
        os.replace(src, dst)
    return dst
