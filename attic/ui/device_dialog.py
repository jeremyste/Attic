"""Modal drive-selection dialog used in the HDD photo-first workflow.

Shown AFTER the photo + physical label are captured, so the user can photograph
and label the drive first, then dock it, refresh, and select it here.

Carries the same safety model as the inline dropdown: only removable/USB disks by
default, with an explicit override (⚠-flagged) for drives that mis-report as
internal.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..core import devices


class DeviceSelectionDialog(QDialog):
    """Pick a target drive; returns it via :meth:`selected_device`."""

    def __init__(self, parent=None, *, show_all: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Select drive")
        self._devices: list[devices.BlockDevice] = []

        prompt = QLabel("Dock/insert the drive now, then select it below.")
        prompt.setWordWrap(True)

        self.combo = QComboBox()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        row = QHBoxLayout()
        row.addWidget(self.combo, 1)
        row.addWidget(self.refresh_btn)

        self.show_all_check = QCheckBox(
            "Show all drives (advanced — includes internal/system disks)"
        )
        self.show_all_check.setChecked(show_all)
        self.show_all_check.toggled.connect(self._on_show_all)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(prompt)
        layout.addLayout(row)
        layout.addWidget(self.show_all_check)
        layout.addWidget(self.buttons)

        self.refresh()

    def _on_show_all(self, checked: bool) -> None:
        if checked:
            QMessageBox.warning(
                self, "Show all drives",
                "Enabling ALL drives includes this computer's own internal and "
                "system disks. Non-removable drives are marked ⚠ and require an "
                "extra confirmation before any read.",
            )
        self.refresh()

    def refresh(self) -> None:
        self._devices = (
            devices.list_all_devices() if self.show_all_check.isChecked()
            else devices.list_removable_devices()
        )
        self.combo.clear()
        for d in self._devices:
            self.combo.addItem(d.label, d)
        if not self._devices:
            self.combo.addItem("No removable/USB drives detected", None)

    def selected_device(self) -> devices.BlockDevice | None:
        return self.combo.currentData()
