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

Each submitted job also gets a :class:`_JobControl`, letting the UI intervene
while it's in flight (see ``FinalizePool.cancel``/``skip_compression``):

- **Cancel** stops the job outright. A queued-but-not-started job is pulled
  straight out of the pool via ``QThreadPool.tryTake`` (cheap: its ``run()``
  never executes at all); an already-running one is asked to stop via a
  ``threading.Event`` that ``compression.compress_and_checksum`` polls, which
  terminates the ``zstd`` subprocess. Either way the staging directory
  (including the big raw image) is discarded and a ``cancelled`` catalog row
  records that it was an intentional stop, not a silent loss.
- **Skip compression** keeps the capture but drops the slow ``-19 --long``
  pass: the raw image itself becomes the archived artifact (uncompressed,
  larger, but immediately available -- no zstd run to wait out). A queued job
  is moved to a small dedicated pool so it does not have to wait behind other
  jobs' compression; a running one has its zstd process killed and falls back
  to the same raw-only path.
"""

from __future__ import annotations

import os
import shutil
import threading
import traceback
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from ..core import catalog, compression, staging
from ..core.catalog import CatalogRow
from ..core.compression import CompressionCancelled
from ..core.config import MediaType, Status
from ..core.staging import StagingDir

# Threads reserved for jobs that skipped compression while still queued on the
# main pool -- kept small and separate so they are not stuck behind other
# jobs' CPU-heavy (``-T0``, all-cores) zstd runs.
_FAST_POOL_THREADS = 2


@dataclass
class FinalizeRequest:
    """Inputs to one finalize job."""

    working_folder: str
    media_type: MediaType
    staging: StagingDir
    raw_image_path: str  # inside staging (any working name); "" if none decoded
    chosen_name: str  # final top-level folder name for this item/drive
    rows: list[CatalogRow]  # one (single volume) or many (HDD partitions)
    # Preserved flux stream to archive alongside the image. Always compressed and
    # the uncompressed copy always dropped -- it is ~40x the image and the .zst
    # is what gets kept, so retaining both would dwarf the archive.
    flux_path: str = ""
    log_path: str = ""  # staged ddrescue/gw logfile to rename to {chosen}.log
    # webcam photos to copy in, mapped {filename_suffix: temp_path}
    photos: dict[str, str] = field(default_factory=dict)
    keep_raw: bool = False  # normally False — only the compressed image is kept
    zstd_level: int = 19
    zstd_long: bool = True
    # When True (AppSettings.hdd_auto_skip_image_when_clean /
    # optical_auto_skip_image_when_clean), a fully clean + fully extracted job
    # never gets an image archived at all -- see _read_and_extraction_are_clean.
    # Checked before compression runs, so it also skips the compression work
    # itself, not just the kept file. Overrides keep_raw when both apply.
    skip_image_when_clean: bool = False


class _Signals(QObject):
    progress = pyqtSignal(str, str)  # (chosen_name, stage text)
    done = pyqtSignal(str, object)  # (final_dir, rows)
    failed = pyqtSignal(str, str)  # (chosen_name, error summary)
    cancelled = pyqtSignal(str)  # (chosen_name,)


@dataclass
class _JobControl:
    """Cooperative-cancellation handle for one in-flight finalize job."""

    cancel_event: threading.Event = field(default_factory=threading.Event)
    skip_event: threading.Event = field(default_factory=threading.Event)
    task: "_FinalizeTask | None" = None  # set right after construction


class _FinalizeTask(QRunnable):
    def __init__(self, req: FinalizeRequest, signals: _Signals, control: _JobControl):
        super().__init__()
        self.req = req
        self.signals = signals
        self.control = control
        self.setAutoDelete(False)  # pool never owns/frees this; FinalizePool does

    def _should_stop(self) -> bool:
        return self.control.cancel_event.is_set() or self.control.skip_event.is_set()

    def run(self) -> None:  # QRunnable entry point (pool thread)
        req = self.req
        if self.control.cancel_event.is_set():
            self._cleanup_cancelled()
            return
        try:
            # Rename staged artifacts to the resolved {chosen_name} before work.
            if req.log_path and os.path.exists(req.log_path):
                _rename_to(req.log_path, req.staging.child(f"{req.chosen_name}.log"))

            result = None
            raw_path = ""
            skipped_compression = False
            image_skipped_clean = False
            discarded_raw_size = 0
            if req.raw_image_path and os.path.exists(req.raw_image_path):
                raw_path = _rename_to(
                    req.raw_image_path, req.staging.child(f"{req.chosen_name}.img")
                )
                if req.skip_image_when_clean and _read_and_extraction_are_clean(req):
                    image_skipped_clean = True
                    discarded_raw_size = os.path.getsize(raw_path)
                    _remove_quiet(raw_path)
                    raw_path = ""
                    self.signals.progress.emit(
                        req.chosen_name,
                        "Read clean, extraction ok -- discarding image",
                    )
                else:
                    result, skipped_compression = self._compress_or_skip(
                        raw_path, req.staging.child(f"{req.chosen_name}.img.zst"),
                        stage_label="image",
                    )
                    if result is None:  # cancelled mid-compression
                        self._cleanup_cancelled()
                        return
                    if not req.keep_raw and not skipped_compression:
                        _remove_quiet(raw_path)

            flux_result = None
            flux_skipped = False
            if req.flux_path and os.path.exists(req.flux_path):
                flux_raw = _rename_to(
                    req.flux_path, req.staging.child(f"{req.chosen_name}.scp")
                )
                flux_result, flux_skipped = self._compress_or_skip(
                    flux_raw, req.staging.child(f"{req.chosen_name}.scp.zst"),
                    stage_label="flux",
                )
                if flux_result is None:
                    self._cleanup_cancelled()
                    return
                if not flux_skipped:
                    _remove_quiet(flux_raw)

            if result is None and flux_result is None and not image_skipped_clean:
                raise RuntimeError("nothing to archive: no image and no flux")

            for suffix, src in req.photos.items():
                if src and os.path.exists(src):
                    shutil.copy2(src, req.staging.child(f"{req.chosen_name}{suffix}"))

            final_dir = staging.final_dir(
                req.working_folder, req.media_type, req.chosen_name
            )
            rel_folder = os.path.relpath(final_dir, req.working_folder)

            skip_note = ""
            for row in req.rows:
                row.folder_path = rel_folder
                if image_skipped_clean:
                    # raw_image_filename/compressed_image_filename stay blank
                    # -- that's how a future reader (and hdd_archive.py's
                    # "deletable" check) tells "no image was ever kept" apart
                    # from "the image was archived and later deleted". The
                    # size is still worth recording even though nothing of
                    # that size survives on disk.
                    row.raw_size_bytes = str(discarded_raw_size)
                    row.notes = (
                        row.notes + " | "
                        "Image not archived: read was fully clean and "
                        "extraction succeeded, so only Extracted Files/ was "
                        "kept (auto-skip enabled)."
                    ).strip(" |")
                if result is not None:
                    row.raw_image_filename = (
                        os.path.basename(raw_path)
                        if (req.keep_raw or skipped_compression) else ""
                    )
                    row.compressed_image_filename = os.path.basename(
                        result.compressed_path
                    )
                    row.raw_size_bytes = str(result.raw_size_bytes)
                    row.compressed_size_bytes = str(result.compressed_size_bytes)
                    row.sha256_raw = result.sha256_raw
                    row.sha256_compressed = result.sha256_compressed
                if flux_result is not None:
                    # sha256_flux_raw is the hash of the *decompressed* stream --
                    # what you verify against after unpacking the .zst later.
                    row.flux_filename = os.path.basename(flux_result.compressed_path)
                    row.flux_raw_size_bytes = str(flux_result.raw_size_bytes)
                    row.flux_compressed_size_bytes = str(
                        flux_result.compressed_size_bytes
                    )
                    row.sha256_flux_raw = flux_result.sha256_raw
                    row.sha256_flux_compressed = flux_result.sha256_compressed
                if skipped_compression or flux_skipped:
                    skip_note = (
                        "Compression skipped by user -- kept uncompressed "
                        + ("image" if skipped_compression else "")
                        + (" and " if skipped_compression and flux_skipped else "")
                        + ("flux" if flux_skipped else "") + "."
                    )
                    row.notes = (row.notes + " | " + skip_note).strip(" |")

            self.signals.progress.emit(req.chosen_name, "Finalizing")
            staging.promote(req.staging, final_dir)
            catalog.append_rows(req.working_folder, req.rows)

            self.signals.done.emit(final_dir, req.rows)
        except Exception as exc:  # noqa: BLE001 - never crash the pool thread
            summary = f"{type(exc).__name__}: {exc}"
            self._record_failure(summary, traceback.format_exc())
            self.signals.failed.emit(req.chosen_name, summary)

    def _compress_or_skip(
        self, raw_path: str, out_path: str, *, stage_label: str,
    ) -> tuple[compression.CompressionResult | None, bool]:
        """Compress ``raw_path``, or keep it uncompressed if skip/cancel wins.

        Returns ``(result, skipped)``. ``result`` is ``None`` only when a full
        cancel (not skip) stopped it -- the caller treats that as "abandon
        the whole job."
        """
        req = self.req
        if self.control.skip_event.is_set():
            self.signals.progress.emit(
                req.chosen_name, f"Skipping compression (keeping raw {stage_label})"
            )
            return compression.raw_only_result(raw_path), True

        self.signals.progress.emit(req.chosen_name, f"Compressing {stage_label}")
        try:
            result = compression.compress_and_checksum(
                raw_path, out_path, level=req.zstd_level, long_mode=req.zstd_long,
                should_cancel=self._should_stop,
            )
            return result, False
        except CompressionCancelled:
            if self.control.cancel_event.is_set():
                return None, False
            # Skip (not a full cancel) won the race mid-compression: drop the
            # partial .zst and fall back to keeping the raw file as-is.
            _remove_quiet(out_path)
            self.signals.progress.emit(
                req.chosen_name, f"Skipping compression (keeping raw {stage_label})"
            )
            return compression.raw_only_result(raw_path), True

    def _cleanup_cancelled(self) -> None:
        """Discard the whole job: staging dir gone, a cancelled row recorded."""
        req = self.req
        note = (
            "Cancelled by user -- raw image/flux discarded, not archived "
            "(read/extraction results, if any, were not kept)."
        )
        try:
            rows = req.rows or [CatalogRow(
                media_type=req.media_type.value, chosen_name=req.chosen_name,
            )]
            for row in rows:
                row.status = Status.CANCELLED.value
                row.notes = (row.notes + " | " + note).strip(" |")
            catalog.append_rows(req.working_folder, rows)
        except Exception:  # noqa: BLE001 - best effort; still clean up the dir
            pass
        try:
            shutil.rmtree(req.staging.path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        self.signals.cancelled.emit(req.chosen_name)

    def _record_failure(self, summary: str, tb: str) -> None:
        """Append a failed catalog row pointing at the surviving staging dir."""
        req = self.req
        # Absolute, not staging.rel_path(): staging now lives under its own
        # staging_root, which may not be anywhere near the working folder this
        # note gets written into.
        note = f"Temp dir left for inspection: {req.staging.path}"
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

    Signals are exposed on :attr:`signals` for the UI to connect to. Jobs are
    tracked by ``chosen_name`` (the same key the signals already report
    under) so the UI can :meth:`cancel` or :meth:`skip_compression` a job it
    only knows by that name.
    """

    max_threads: int = 0  # 0 -> Qt default (cpu count)
    signals: _Signals = field(default_factory=_Signals)
    _pool: QThreadPool = field(default=None, repr=False)
    _fast_pool: QThreadPool = field(default=None, repr=False)
    _controls: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        self._pool = QThreadPool.globalInstance()
        if self.max_threads:
            self._pool.setMaxThreadCount(self.max_threads)
        self._fast_pool = QThreadPool()
        self._fast_pool.setMaxThreadCount(_FAST_POOL_THREADS)
        # Whatever thread a job finishes on, drop its control entry -- the
        # dict pop is lock-protected, so it's fine to run from a pool thread
        # rather than waiting to be queued back to the GUI thread.
        self.signals.done.connect(lambda _dir, rows: self._forget_by_rows(rows))
        self.signals.failed.connect(lambda name, _err: self._forget(name))
        self.signals.cancelled.connect(lambda name: self._forget(name))

    def submit(self, request: FinalizeRequest) -> None:
        control = _JobControl()
        task = _FinalizeTask(request, self.signals, control)
        control.task = task
        with self._lock:
            self._controls[request.chosen_name] = control
        self._pool.start(task)

    def cancel(self, chosen_name: str) -> bool:
        """Stop a job, queued or running. Returns False if not found."""
        control = self._get(chosen_name)
        if control is None:
            return False
        control.cancel_event.set()
        if self._pool.tryTake(control.task):
            # Never started -- run() will never execute, so do its cleanup here
            # (which itself emits `cancelled`, forgetting the entry).
            control.task._cleanup_cancelled()
        # If already running, the task's own should_cancel checks do the rest.
        return True

    def skip_compression(self, chosen_name: str) -> bool:
        """Drop the compression step for a job, queued or running.

        Returns False if not found (already finished, or never submitted).
        """
        control = self._get(chosen_name)
        if control is None:
            return False
        control.skip_event.set()
        if self._pool.tryTake(control.task):
            # Wasn't running yet -- rerun it (compression will be skipped) on
            # the small dedicated pool instead of the main one, which may be
            # fully occupied by other jobs' all-core zstd runs.
            self._fast_pool.start(control.task)
        return True

    def wait(self, msecs: int = -1) -> bool:
        ok = self._pool.waitForDone(msecs)
        return self._fast_pool.waitForDone(msecs) and ok

    def _get(self, chosen_name: str) -> _JobControl | None:
        with self._lock:
            return self._controls.get(chosen_name)

    def _forget(self, chosen_name: str) -> None:
        with self._lock:
            self._controls.pop(chosen_name, None)

    def _forget_by_rows(self, rows) -> None:
        if rows:
            self._forget(rows[0].chosen_name)


def _read_and_extraction_are_clean(req: FinalizeRequest) -> bool:
    """True when ``req`` is a fully clean, fully extracted result.

    Mirrors ``hdd_archive.py``'s "deletable" criteria (kept in sync
    deliberately -- both answer "is this image genuinely redundant with its
    extraction"), just evaluated pre-write on live ``CatalogRow`` objects
    instead of post-write catalog dict rows:

    * Every row's own extraction ``status`` must be "ok".
    * HDD only: its rows share one drive-level read, recorded once per row as
      ``read_bad_bytes`` (set in AppContext.route_hdd before this request is
      built) -- every row must show a genuinely known-zero count, not blank
      (blank means unrecorded, which is not the same as known-clean).
    * Everything else that could reach here (currently just optical, since
      floppy never sets skip_image_when_clean): a single row whose own status
      already reached "ok" only when ddrescue's read was fully clean AND
      extraction (and DVD-Video conversion, if applicable) fully succeeded --
      see OpticalCaptureWorker.capture -- so status alone is sufficient.
    """
    if not req.rows:
        return False
    if not all(row.status == Status.OK.value for row in req.rows):
        return False
    if req.media_type != MediaType.HDD:
        return True
    bad_values = [row.read_bad_bytes for row in req.rows]
    return bool(bad_values) and all(v.isdigit() and int(v) == 0 for v in bad_values)


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
