"""Shared "Pending Labels" panel, visible regardless of the active tab.

When a volume finishes capture with no confidently detected label AND no physical
label was entered, it is queued here (under its fallback name) instead of blocking
the pipeline. The user can address it whenever; renaming from here updates BOTH
the on-disk folder name and the catalog row(s).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ..core import catalog
from ..core.config import MediaType
from ..core.sanitize import sanitize_filename
from ..core.staging import final_dir


@dataclass
class PendingItem:
    working_folder: str
    media_type: MediaType
    current_name: str  # the fallback name currently on disk + in the catalog


class PendingLabelsPanel(QGroupBox):
    """A list of unlabeled volumes awaiting a name, with a rename action."""

    renamed = pyqtSignal(object, str)  # (PendingItem, new_name)

    def __init__(self, parent=None):
        super().__init__("Pending Labels", parent)
        self._items: list[PendingItem] = []

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._rename_selected())

        self.rename_btn = QPushButton("Rename…")
        self.rename_btn.clicked.connect(self._rename_selected)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.rename_btn)

    def add_pending(self, item: PendingItem) -> None:
        self._items.append(item)
        entry = QListWidgetItem(f"[{item.media_type.value}] {item.current_name}")
        entry.setData(0x0100, len(self._items) - 1)  # Qt.UserRole
        self.list.addItem(entry)

    def _rename_selected(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        item = self._items[row]
        new_name, ok = QInputDialog.getText(
            self, "Rename volume",
            f"New name for '{item.current_name}':", text=item.current_name,
        )
        if not ok:
            return
        new_name = sanitize_filename(new_name.strip())
        if not new_name or new_name == item.current_name:
            return
        self._apply_rename(item, new_name, row)

    def _apply_rename(self, item: PendingItem, new_name: str, row: int) -> None:
        old_dir = final_dir(item.working_folder, item.media_type, item.current_name)
        new_dir = final_dir(item.working_folder, item.media_type, new_name)
        if os.path.exists(new_dir):
            new_name = _dedupe_on_disk(item.working_folder, item.media_type, new_name)
            new_dir = final_dir(item.working_folder, item.media_type, new_name)

        old_rel = os.path.relpath(old_dir, item.working_folder)
        new_rel = os.path.relpath(new_dir, item.working_folder)

        if os.path.exists(old_dir):
            os.rename(old_dir, new_dir)
        catalog.rename_item(
            item.working_folder, item.media_type.value,
            item.current_name, new_name,
            old_folder_path=old_rel, new_folder_path=new_rel,
        )

        item.current_name = new_name
        self.list.item(row).setText(f"[{item.media_type.value}] {new_name}")
        self.renamed.emit(item, new_name)


def _dedupe_on_disk(working_folder: str, media_type: MediaType, name: str) -> str:
    n = 2
    while os.path.exists(final_dir(working_folder, media_type, f"{name}_{n}")):
        n += 1
    return f"{name}_{n}"
