"""Attic entrypoint.

Launches the Qt app, prompts for the session's working folder (remembering the
last one), then opens the main window with the three pipeline tabs.
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from attic.ui.main_window import MainWindow
from attic.ui.session import choose_working_folder


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Attic")
    app.setOrganizationName("Attic")

    session = choose_working_folder()
    if session is None:
        return 0  # user cancelled the folder picker

    window = MainWindow(session)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
