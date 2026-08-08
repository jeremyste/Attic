"""Archived HDD drives: browse what's already been captured, and reclaim space
by deleting a drive's image once its read and extraction were both fully
clean (Extracted Files/ is always kept -- only the big whole-drive image
file goes).
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core import hdd_archive


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


class _ItemRow(QWidget):
    def __init__(self, item: hdd_archive.HddArchiveItem, on_delete, parent=None):
        super().__init__(parent)
        self.item = item

        label_bits = [item.chosen_name]
        if item.partition_count > 1:
            label_bits.append(f"({item.partition_count} partitions)")
        title = QLabel(" ".join(label_bits))
        title.setStyleSheet("font-weight: bold;")

        if item.image_deleted:
            status = QLabel("Image deleted — Extracted Files retained")
        elif item.image_filename:
            status = QLabel(f"Image: {_human_size(item.compressed_size_bytes)}")
        else:
            status = QLabel("No image was archived for this drive")

        self.delete_btn = QPushButton("Delete image")
        self.delete_btn.setEnabled(item.deletable)
        if not item.deletable:
            self.delete_btn.setToolTip(item.not_deletable_reason)
        else:
            self.delete_btn.setToolTip(
                "Frees space by removing the whole-drive image; Extracted "
                "Files stays. Cannot be undone -- re-imaging the drive is "
                "the only way to get the raw image back."
            )
        self.delete_btn.clicked.connect(lambda: on_delete(self.item))

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        text_col = QVBoxLayout()
        text_col.addWidget(title)
        text_col.addWidget(status)
        row.addLayout(text_col, 1)
        row.addWidget(self.delete_btn)


class HddArchivePanel(QGroupBox):
    """List of archived HDD drives, each with a Delete-image action."""

    def __init__(self, working_folder: str, parent=None):
        super().__init__("Archived Drives", parent)
        self.working_folder = working_folder

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.idle = QLabel("No HDD items archived yet.")
        self.idle.setEnabled(False)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)

        top = QHBoxLayout()
        top.addWidget(QLabel("Archived Drives"))
        top.addStretch(1)
        top.addWidget(self.refresh_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.idle)
        layout.addWidget(self.list)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        items = hdd_archive.list_hdd_items(self.working_folder)
        for item in items:
            row = _ItemRow(item, self._on_delete_clicked)
            list_item = QListWidgetItem()
            list_item.setSizeHint(row.sizeHint())
            self.list.addItem(list_item)
            self.list.setItemWidget(list_item, row)
        empty = len(items) == 0
        self.idle.setVisible(empty)
        self.list.setVisible(not empty)

    def _on_delete_clicked(self, item: hdd_archive.HddArchiveItem) -> None:
        reply = QMessageBox.question(
            self, "Delete image",
            f"Delete the archived image for \"{item.chosen_name}\" "
            f"({_human_size(item.compressed_size_bytes)})?\n\n"
            "The read and extraction were both fully clean, and Extracted "
            "Files will be kept -- only the whole-drive image is removed. "
            "This cannot be undone; re-imaging the drive is the only way to "
            "get the raw image back.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        timestamp = datetime.now().isoformat(timespec="seconds")
        result = hdd_archive.delete_hdd_image(
            self.working_folder, item, timestamp=timestamp
        )
        if not result.ok:
            QMessageBox.warning(self, "Delete failed", result.error)
        self.refresh()
