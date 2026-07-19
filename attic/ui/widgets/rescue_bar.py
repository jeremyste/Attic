"""Horizontal ddrescue progress bar (ddrescueview-style).

Renders the mapfile's segments proportionally along a horizontal bar, colour
coded by status (rescued / non-tried / bad-sector / trimmed/other), for the HDD
and optical pipelines.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QWidget

from ...core.ddrescue import MapSummary

_STATUS_COLORS = {
    "+": QColor("#22c55e"),  # rescued
    "?": QColor("#3a3f4b"),  # non-tried
    "-": QColor("#ef4444"),  # bad sector
    "*": QColor("#f59e0b"),  # non-trimmed
    "/": QColor("#eab308"),  # non-scraped
}
_DEFAULT_COLOR = QColor("#64748b")
_EMPTY_COLOR = QColor("#1e222a")


class RescueBar(QWidget):
    """Proportional horizontal map of a ddrescue run."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._summary: MapSummary | None = None
        self.setMinimumHeight(28)

    def set_summary(self, summary: MapSummary) -> None:
        self._summary = summary
        self.update()

    def clear(self) -> None:
        self._summary = None
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(400, 28)

    def paintEvent(self, ev) -> None:
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, _EMPTY_COLOR)
        summary = self._summary
        if not summary or summary.total_bytes <= 0:
            return
        # Draw segments ordered by position for a spatially faithful map.
        width = rect.width()
        total = summary.total_bytes
        x = 0.0
        for seg in sorted(summary.segments, key=lambda s: s.pos):
            seg_w = width * (seg.size / total)
            color = _STATUS_COLORS.get(seg.status, _DEFAULT_COLOR)
            painter.fillRect(
                int(x), rect.top(), max(int(seg_w), 1), rect.height(), color
            )
            x += seg_w
