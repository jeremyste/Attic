"""Cylinder × head track grid for the floppy pipeline.

A live grid where each cell is one (cylinder, head) track, colour-coded by read
result as gw reports them: clean read, needed retries, or failed.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QWidget

from ...controllers.floppy import TRACK_CLEAN, TRACK_FAILED, TRACK_RETRIED

_COLORS = {
    None: QColor("#3a3f4b"),          # not yet read
    TRACK_CLEAN: QColor("#22c55e"),   # green
    TRACK_RETRIED: QColor("#f59e0b"), # amber
    TRACK_FAILED: QColor("#ef4444"),  # red
}


class TrackGrid(QWidget):
    """Grid of ``cylinders × heads`` cells, updated per track."""

    def __init__(self, cylinders: int = 80, heads: int = 2, parent=None):
        super().__init__(parent)
        self.cylinders = cylinders
        self.heads = heads
        # status[cyl][head] -> one of the _COLORS keys
        self._status: list[list[str | None]] = [
            [None] * heads for _ in range(cylinders)
        ]
        self.setMinimumHeight(60)

    def reset(self, cylinders: int | None = None, heads: int | None = None) -> None:
        if cylinders:
            self.cylinders = cylinders
        if heads:
            self.heads = heads
        self._status = [[None] * self.heads for _ in range(self.cylinders)]
        self.update()

    def set_track(self, cyl: int, head: int, status: str) -> None:
        if 0 <= cyl < self.cylinders and 0 <= head < self.heads:
            self._status[cyl][head] = status
            self.update()

    def counts(self) -> dict[str, int]:
        out = {TRACK_CLEAN: 0, TRACK_RETRIED: 0, TRACK_FAILED: 0}
        for col in self._status:
            for s in col:
                if s in out:
                    out[s] += 1
        return out

    def sizeHint(self) -> QSize:
        return QSize(self.cylinders * 8, self.heads * 24)

    def paintEvent(self, ev) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1e222a"))
        w = self.width() / max(self.cylinders, 1)
        h = self.height() / max(self.heads, 1)
        for cyl in range(self.cylinders):
            for head in range(self.heads):
                color = _COLORS.get(self._status[cyl][head], _COLORS[None])
                x = int(cyl * w)
                y = int(head * h)
                painter.fillRect(
                    x, y, max(int(w) - 1, 1), max(int(h) - 1, 1), color
                )
