"""Attic entrypoint.

Launches the Qt app, prompts for the session's working folder (remembering the
last one), then opens the main window with the three pipeline tabs.
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication, QMessageBox

from attic.core import priv_client
from attic.ui.main_window import MainWindow
from attic.ui.session import choose_working_folder


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Attic")
    app.setOrganizationName("Attic")

    session = choose_working_folder()
    if session is None:
        return 0  # user cancelled the folder picker

    # One pkexec prompt, here, up front -- authorizes the privileged helper
    # for the whole session so ddrescue/mount calls later never prompt again.
    # (Dismissing this one prompt is recoverable: every privileged call falls
    # back to a per-call pkexec prompt if the helper isn't running.)
    try:
        priv_client.ensure_running()
    except priv_client.HelperUnavailable as exc:
        QMessageBox.warning(
            None, "Privileged helper unavailable",
            "Could not start the privileged helper for disk/mount access:\n\n"
            f"{exc}\n\n"
            "Attic will keep working, but every mount/ddrescue operation will "
            "prompt for authorization individually instead of just once.",
        )

    window = MainWindow(session)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
