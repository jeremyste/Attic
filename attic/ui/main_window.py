"""Main window: three pipeline tabs + an always-visible Pending Labels panel.

Each tab is independently operable (starting an HDD rescue never blocks the floppy
or optical tabs — every capture runs in its own QThread and compression runs in a
shared pool). The Pending Labels panel is docked so it stays visible regardless of
the active tab.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QDockWidget,
    QMainWindow,
    QStatusBar,
    QTabWidget,
)

from ..controllers.compress_pool import FinalizePool
from ..core.settings import load_settings, save_settings
from .app_context import AppContext
from .floppy_tab import FloppyTab
from .hdd_tab import HddTab
from .optical_tab import OpticalTab
from .pending_labels_panel import PendingLabelsPanel
from .session import Session
from .settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    def __init__(self, session: Session):
        super().__init__()
        self.setWindowTitle(f"Attic — {session.working_folder}")
        self.resize(1000, 720)

        self.finalize_pool = FinalizePool()
        self.pending_panel = PendingLabelsPanel()
        self.context = AppContext(
            session=session,
            finalize_pool=self.finalize_pool,
            pending_panel=self.pending_panel,
            settings=load_settings(session.working_folder),
            parent=self,
        )

        self.tabs = QTabWidget()
        self.tabs.addTab(FloppyTab(self.context), "Floppy")
        self.tabs.addTab(HddTab(self.context), "HDD")
        self.tabs.addTab(OpticalTab(self.context), "Optical")
        self.setCentralWidget(self.tabs)

        self._build_menu()

        dock = QDockWidget("Pending Labels", self)
        dock.setWidget(self.pending_panel)
        dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(f"Working folder: {session.working_folder}")

        # Finalize-pool signals are cross-thread; Qt queues them to the GUI thread.
        self.finalize_pool.signals.progress.connect(
            lambda name, stage: self.statusBar().showMessage(f"{name}: {stage}")
        )
        self.finalize_pool.signals.done.connect(self._on_finalize_done)
        self.finalize_pool.signals.failed.connect(
            lambda name, err: self.statusBar().showMessage(f"{name}: FAILED — {err}")
        )

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&File")
        settings_action = QAction("&Settings…", self)
        settings_action.triggered.connect(self._open_settings)
        menu.addAction(settings_action)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.context.settings, parent=self)
        if not dlg.exec():
            return
        self.context.settings = dlg.result_settings()
        save_settings(self.context.session.working_folder, self.context.settings)
        # Push settings that affect already-built widgets.
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if hasattr(tab, "apply_settings"):
                tab.apply_settings()
        self.statusBar().showMessage("Settings saved to working folder.")

    def _on_finalize_done(self, final_dir: str, rows) -> None:
        self.context.on_finalize_done(final_dir, rows)
        self.statusBar().showMessage(f"Archived: {final_dir}")

    def closeEvent(self, ev) -> None:
        # Let in-flight compression finish before exit so nothing is left partial.
        self.statusBar().showMessage("Waiting for background compression to finish…")
        self.finalize_pool.wait(30000)
        super().closeEvent(ev)
