"""Session setup: the destination working folder and its on-disk skeleton.

On launch the user picks one working folder; everything for the session lives
inside it (no auto-created person/project subfolders — the folder itself is the
scope). The last-used path is remembered via QSettings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QFileDialog, QWidget

from ..core import catalog
from ..core.config import MediaType, TMP_DIRNAME

_ORG = "Attic"
_APP = "Attic"
_LAST_FOLDER_KEY = "last_working_folder"


@dataclass
class Session:
    """The active working folder plus helpers to build its layout."""

    working_folder: str

    def ensure_skeleton(self) -> None:
        """Create catalog.csv, the per-type folders, and .tmp/ if missing."""
        os.makedirs(self.working_folder, exist_ok=True)
        for mt in MediaType:
            os.makedirs(os.path.join(self.working_folder, mt.folder_name), exist_ok=True)
        os.makedirs(os.path.join(self.working_folder, TMP_DIRNAME), exist_ok=True)
        catalog.ensure_catalog(self.working_folder)

    @property
    def catalog_path(self) -> str:
        return catalog.catalog_path(self.working_folder)


def _settings() -> QSettings:
    return QSettings(_ORG, _APP)


def last_working_folder() -> str:
    return _settings().value(_LAST_FOLDER_KEY, "", type=str)


def remember_working_folder(path: str) -> None:
    _settings().setValue(_LAST_FOLDER_KEY, path)


def choose_working_folder(parent: QWidget | None = None) -> Session | None:
    """Prompt for a working folder (defaulting to the last-used), build its
    skeleton, and return a :class:`Session`. Returns None if cancelled.
    """
    start_dir = last_working_folder() or os.path.expanduser("~")
    path = QFileDialog.getExistingDirectory(
        parent, "Choose working folder for this archiving session", start_dir
    )
    if not path:
        return None
    session = Session(working_folder=path)
    session.ensure_skeleton()
    remember_working_folder(path)
    return session
