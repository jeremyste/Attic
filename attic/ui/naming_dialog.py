"""Post-capture naming dialog (shared by all pipelines).

Shows the three name sources (physical label entered, detected label, and the
auto-generated fallback) so the user can see where they disagree, lets the user
choose/edit the final name, and — when the volume date used for the fallback
looks suspect (dead-CMOS-battery symptom) — surfaces a warning and lets the date
be overridden.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..core.config import MediaType
from ..core.sanitize import sanitize_filename


@dataclass
class NamingOutcome:
    chosen_name: str
    fallback_date: str


class NamingDialog(QDialog):
    """Confirm/adjust the resolved name for one captured volume."""

    def __init__(
        self,
        media_type: MediaType,
        *,
        physical_label: str,
        detected_label: str,
        suggested_name: str,
        fallback_date: str = "",
        date_suspect: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Name this {media_type.value}")
        self._physical = physical_label
        self._detected = detected_label

        form = QFormLayout()
        form.addRow("Physical label entered:", _readonly(physical_label or "—"))
        form.addRow("Detected label:", _readonly(detected_label or "—"))

        self.name_edit = QLineEdit(suggested_name)
        form.addRow("Chosen name:", self.name_edit)

        self.date_edit = QLineEdit(fallback_date)
        self.date_edit.setPlaceholderText("YYYY-MM-DD")
        form.addRow("Fallback date:", self.date_edit)

        layout = QVBoxLayout(self)
        layout.addLayout(form)

        if date_suspect:
            warn = QLabel(
                "⚠ The detected volume date looks suspect (out of the 1980–today "
                "range — often a dead CMOS battery). Please verify or override it."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #b45309;")
            layout.addWidget(warn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def outcome(self) -> NamingOutcome:
        """Return the sanitized chosen name and (possibly edited) fallback date."""
        return NamingOutcome(
            chosen_name=sanitize_filename(self.name_edit.text().strip()),
            fallback_date=self.date_edit.text().strip(),
        )


def _readonly(text: str) -> QLineEdit:
    field = QLineEdit(text)
    field.setReadOnly(True)
    return field
