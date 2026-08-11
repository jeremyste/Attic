"""Shared "Processing" panel: what the background pipeline is chewing on.

A capture job outlives the drive read that started it -- decode, extraction,
compression and promotion all continue after the media has been ejected, and
several jobs can be in flight across all three pipelines at once. Without
somewhere to show that, the only feedback is a single status-bar line that the
next job immediately overwrites.

Jobs are tracked by their staging session id, which exists from the moment the
capture starts. The final name is only resolved later (after detection), so
:meth:`rename_job` re-labels the row in place and registers the chosen name as
an alias -- that is the key the finalize pool reports progress under.

Progress is only ever shown when it is real. Stages with a genuine total (the
gw track count, ddrescue's rescued fraction) drive a determinate bar; stages
with no measurable total (extraction, zstd) show a busy bar rather than a
fabricated percentage.

Per-job actions (give up on the current step, cancel the whole job outright)
live behind a double-click popup rather than always-visible row buttons -- one
click target per job instead of two, and room for a plain-language description
of what each choice actually does before it happens.
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.config import MediaType

# How long a finished job stays visible before it is cleared, in milliseconds.
DONE_LINGER_MS = 6000

# Short tag per pipeline, so one queue can mix all three.
_TAGS = {
    MediaType.FLOPPY: "FLOPPY",
    MediaType.HDD: "HDD",
    MediaType.OPTICAL: "CD/DVD",
}


def media_tag(media_type: MediaType) -> str:
    return _TAGS.get(media_type, str(getattr(media_type, "value", media_type)).upper())


class JobActionDialog(QDialog):
    """Popup opened by double-clicking a Processing row.

    Offers up to two concrete actions plus an always-present "Dismiss" (do
    nothing, keep going) -- the caller supplies labels/descriptions for
    whichever actions apply to this job's current stage, and omits the ones
    that don't (e.g. a job still being captured has nothing to "skip
    compression" on). After :meth:`exec`, ``action`` is "skip", "cancel", or
    None (dismissed / closed).
    """

    def __init__(
        self, title: str, status_text: str, *,
        skip_label: str = "", skip_detail: str = "",
        cancel_label: str = "", cancel_detail: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.action: str | None = None

        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet("font-weight: bold;")
        layout.addWidget(heading)
        layout.addWidget(QLabel(status_text))

        if skip_label:
            layout.addWidget(_action_button(skip_label, skip_detail, self._choose_skip))
        if cancel_label:
            layout.addWidget(_action_button(cancel_label, cancel_detail, self._choose_cancel))

        dismiss = QPushButton("Dismiss (do nothing, keep going)")
        dismiss.clicked.connect(self.reject)
        layout.addWidget(dismiss)

    def _choose_skip(self) -> None:
        self.action = "skip"
        self.accept()

    def _choose_cancel(self) -> None:
        self.action = "cancel"
        self.accept()


def _action_button(label: str, detail: str, on_click) -> QWidget:
    """An action button plus a small greyed-out line explaining it."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    btn = QPushButton(label)
    btn.clicked.connect(on_click)
    layout.addWidget(btn)
    if detail:
        note = QLabel(detail)
        note.setWordWrap(True)
        note.setEnabled(False)
        layout.addWidget(note)
    return box


class _JobRow(QWidget):
    """One job: '[TAG] name', its current stage, and a progress bar.

    ``stage_kind`` tracks which pipeline phase the job is in --
    "capturing" (imaging/decoding/extraction), "finalizing" (in the
    compression pool), or "done" -- since that determines which actions the
    double-click popup offers and which key (job id vs. chosen name) they get
    emitted under.
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = QLabel(title)
        self.title.setStyleSheet("font-weight: bold;")
        self.stage = QLabel("starting")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.stage_kind = "capturing"
        # Whether this job's owning tab can actually act on a capture-phase
        # skip/cancel (currently: HDD and optical, both ddrescue-driven --
        # not floppy, whose gw read is comparatively quick).
        self.supports_capture_control = False

        hint = QLabel("Double-click for options")
        hint.setStyleSheet("color: gray; font-size: 10px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 6)
        layout.setSpacing(2)
        layout.addWidget(self.title)
        layout.addWidget(self.stage)
        layout.addWidget(self.bar)
        layout.addWidget(hint)
        self.set_busy()

    def set_busy(self) -> None:
        """Indeterminate: work is happening but has no meaningful total."""
        self.bar.setRange(0, 0)

    def set_percent(self, percent: int) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(max(0, min(100, percent)))

    def set_done(self) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(100)


class ProcessingPanel(QGroupBox):
    """Live list of in-flight jobs across every pipeline."""

    # (job_id,) -- a capture-phase skip/cancel. Only the owning tab knows how
    # to act on these (which worker to signal), so it connects them itself
    # rather than this panel resolving them directly.
    capture_skip_requested = pyqtSignal(str)
    capture_cancel_requested = pyqtSignal(str)
    # (chosen_name,) -- connect these to FinalizePool.cancel/skip_compression.
    compress_skip_requested = pyqtSignal(str)
    compress_cancel_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("Processing", parent)
        self._rows: dict[str, _JobRow] = {}
        self._items: dict[str, QListWidgetItem] = {}
        self._alias: dict[str, str] = {}  # chosen_name -> job_id
        self._names: dict[str, str] = {}  # job_id -> display name
        self._tags: dict[str, str] = {}  # job_id -> media tag

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.list.itemDoubleClicked.connect(self._open_job_dialog)
        self.idle = QLabel("Nothing processing.")
        self.idle.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self.idle)
        layout.addWidget(self.list, 1)
        self._refresh_idle()

    # --- lifecycle ----------------------------------------------------------

    def start_job(
        self, job_id: str, media_type: MediaType, display_name: str,
        *, supports_capture_control: bool = False,
    ) -> None:
        if job_id in self._rows:
            return
        tag = media_tag(media_type)
        self._names[job_id] = display_name
        self._tags[job_id] = tag

        row = _JobRow(f"[{tag}] {display_name}")
        row.supports_capture_control = supports_capture_control
        item = QListWidgetItem()
        item.setSizeHint(row.sizeHint())
        self.list.addItem(item)
        self.list.setItemWidget(item, row)
        self._rows[job_id] = row
        self._items[job_id] = item
        self._refresh_idle()

    def set_stage(self, job_id: str, stage: str) -> None:
        row = self._rows.get(job_id)
        if row is None:
            return
        row.stage.setText(stage)
        # A new stage has no measurement until one arrives.
        row.set_busy()

    def set_percent(self, job_id: str, percent: int) -> None:
        row = self._rows.get(job_id)
        if row is not None:
            row.set_percent(percent)

    def rename_job(self, job_id: str, chosen_name: str) -> None:
        """Adopt the resolved name, and remember it as an alias for the pool."""
        row = self._rows.get(job_id)
        if row is None:
            return
        self._names[job_id] = chosen_name
        self._alias[chosen_name] = job_id
        row.title.setText(f"[{self._tags[job_id]}] {chosen_name}")
        self.set_stage(job_id, "queued for compression")
        # From here on the job has a raw image sitting in the finalize pool,
        # so the popup switches from capture actions to compression ones.
        row.stage_kind = "finalizing"

    def finish_job(self, job_id: str, note: str = "done") -> None:
        row = self._rows.get(job_id)
        if row is None:
            return
        row.stage_kind = "done"
        row.stage.setText(note)
        row.set_done()
        QTimer.singleShot(DONE_LINGER_MS, lambda: self._drop(job_id))

    def _open_job_dialog(self, item: QListWidgetItem) -> None:
        job_id = next((jid for jid, it in self._items.items() if it is item), None)
        if job_id is None:
            return
        row = self._rows[job_id]
        name = self._names[job_id]
        status_text = f"Currently: {row.stage.text()}"

        if row.stage_kind == "capturing" and row.supports_capture_control:
            dlg = JobActionDialog(
                name, status_text,
                skip_label="Give up on this step, keep going",
                skip_detail=(
                    "Stop the current read right now and continue the "
                    "pipeline with whatever has been recovered so far "
                    "(extraction, then optional compression)."
                ),
                cancel_label="Cancel entirely",
                cancel_detail=(
                    "Stop and discard everything captured so far -- nothing "
                    "from this job will be archived."
                ),
                parent=self,
            )
            dlg.exec()
            if dlg.action == "skip":
                self.capture_skip_requested.emit(job_id)
            elif dlg.action == "cancel":
                self.capture_cancel_requested.emit(job_id)
        elif row.stage_kind == "capturing":
            QMessageBox.information(
                self, name, f"{name}\n\n{status_text}\n\n"
                "No cancel controls are available for this stage.",
            )
        elif row.stage_kind == "finalizing":
            dlg = JobActionDialog(
                name, status_text,
                skip_label="Skip compression",
                skip_detail=(
                    "Keep the raw (uncompressed) image instead of waiting "
                    "for compression to finish -- larger on disk, but ready "
                    "immediately."
                ),
                cancel_label="Cancel entirely",
                cancel_detail=(
                    "Stop this job and discard its raw image -- nothing "
                    "from this capture will be archived."
                ),
                parent=self,
            )
            dlg.exec()
            if dlg.action == "skip":
                self.compress_skip_requested.emit(name)
            elif dlg.action == "cancel":
                self.compress_cancel_requested.emit(name)
        else:
            QMessageBox.information(self, name, f"{name}\n\n{status_text}")

    # --- keyed by the resolved name (what the finalize pool reports) --------

    def set_stage_by_name(self, chosen_name: str, stage: str) -> None:
        job_id = self._alias.get(chosen_name)
        if job_id:
            self.set_stage(job_id, stage)

    def finish_by_name(self, chosen_name: str, note: str = "done") -> None:
        job_id = self._alias.get(chosen_name)
        if job_id:
            self.finish_job(job_id, note)

    # --- internals ----------------------------------------------------------

    def _drop(self, job_id: str) -> None:
        item = self._items.pop(job_id, None)
        if item is not None:
            row = self.list.row(item)
            if row >= 0:
                self.list.takeItem(row)
        self._rows.pop(job_id, None)
        self._tags.pop(job_id, None)
        name = self._names.pop(job_id, None)
        if name is not None:
            self._alias.pop(name, None)
        self._refresh_idle()

    def _refresh_idle(self) -> None:
        empty = self.list.count() == 0
        self.idle.setVisible(empty)
        self.list.setVisible(not empty)

    # --- for tests ----------------------------------------------------------

    def rows(self) -> list[str]:
        out = []
        for i in range(self.list.count()):
            row = self.list.itemWidget(self.list.item(i))
            out.append(f"{row.title.text()} - {row.stage.text()}")
        return out
