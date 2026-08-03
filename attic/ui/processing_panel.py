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
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
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


class _JobRow(QWidget):
    """One job: '[TAG] name', its current stage, and a progress bar."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = QLabel(title)
        self.title.setStyleSheet("font-weight: bold;")
        self.stage = QLabel("starting")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 6)
        layout.setSpacing(2)
        layout.addWidget(self.title)
        layout.addWidget(self.stage)
        layout.addWidget(self.bar)
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

    def __init__(self, parent=None):
        super().__init__("Processing", parent)
        self._rows: dict[str, _JobRow] = {}
        self._items: dict[str, QListWidgetItem] = {}
        self._alias: dict[str, str] = {}  # chosen_name -> job_id
        self._names: dict[str, str] = {}  # job_id -> display name
        self._tags: dict[str, str] = {}  # job_id -> media tag

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.idle = QLabel("Nothing processing.")
        self.idle.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self.idle)
        layout.addWidget(self.list, 1)
        self._refresh_idle()

    # --- lifecycle ----------------------------------------------------------

    def start_job(self, job_id: str, media_type: MediaType, display_name: str) -> None:
        if job_id in self._rows:
            return
        tag = media_tag(media_type)
        self._names[job_id] = display_name
        self._tags[job_id] = tag

        row = _JobRow(f"[{tag}] {display_name}")
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

    def finish_job(self, job_id: str, note: str = "done") -> None:
        row = self._rows.get(job_id)
        if row is None:
            return
        row.stage.setText(note)
        row.set_done()
        QTimer.singleShot(DONE_LINGER_MS, lambda: self._drop(job_id))

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
