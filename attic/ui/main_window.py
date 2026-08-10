"""Main window: three pipeline tabs + the Processing and Pending Labels panels.

Each tab is independently operable (starting an HDD rescue never blocks the floppy
or optical tabs; every capture runs in its own QThread and compression runs in a
shared pool). Both panels are docked so they stay visible regardless of the
active tab.

A job outlives the tab that started it: the drive is released as soon as the
hardware is done, so captures overlap. The Processing panel is the only place
that shows the whole picture, which is why the finalize pool's signals are
forwarded to it here as well as to the status bar.
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
from .processing_panel import ProcessingPanel
from .session import Session
from .settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    def __init__(self, session: Session):
        super().__init__()
        self.setWindowTitle(f"Attic — {session.working_folder}")
        self.resize(1000, 720)

        self.finalize_pool = FinalizePool()
        self.pending_panel = PendingLabelsPanel()
        self.processing_panel = ProcessingPanel()
        self.context = AppContext(
            session=session,
            finalize_pool=self.finalize_pool,
            pending_panel=self.pending_panel,
            processing_panel=self.processing_panel,
            settings=load_settings(session.working_folder),
            parent=self,
        )

        self.tabs = QTabWidget()
        self.tabs.addTab(FloppyTab(self.context), "Floppy")
        self.tabs.addTab(HddTab(self.context), "HDD")
        self.tabs.addTab(OpticalTab(self.context), "Optical")
        self.setCentralWidget(self.tabs)

        self._build_menu()

        areas = (
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        proc_dock = QDockWidget("Processing", self)
        proc_dock.setWidget(self.processing_panel)
        proc_dock.setAllowedAreas(areas)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, proc_dock)

        dock = QDockWidget("Pending Labels", self)
        dock.setWidget(self.pending_panel)
        dock.setAllowedAreas(areas)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(
            f"Working folder: {session.working_folder}  |  "
            f"Staging: {self.context.staging_root}"
        )

        # Finalize-pool signals are cross-thread; Qt queues them to the GUI thread.
        # The pool reports under the resolved name, which the panel knows as an
        # alias for the job it has been tracking since the capture began.
        self.finalize_pool.signals.progress.connect(self._on_finalize_progress)
        self.finalize_pool.signals.done.connect(self._on_finalize_done)
        self.finalize_pool.signals.failed.connect(self._on_finalize_failed)
        self.finalize_pool.signals.cancelled.connect(self._on_finalize_cancelled)
        # The panel's Cancel/Skip buttons only know a job's chosen name; the
        # pool is what actually holds the cancellation handle for it.
        self.processing_panel.cancel_requested.connect(self.finalize_pool.cancel)
        self.processing_panel.skip_requested.connect(
            self.finalize_pool.skip_compression
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
        self.statusBar().showMessage(
            f"Settings saved to working folder. Staging: {self.context.staging_root}"
        )

    def _on_finalize_progress(self, name: str, stage: str) -> None:
        self.processing_panel.set_stage_by_name(name, stage)
        self.statusBar().showMessage(f"{name}: {stage}")

    def _on_finalize_failed(self, name: str, err: str) -> None:
        self.processing_panel.finish_by_name(name, f"FAILED - {err}")
        self.statusBar().showMessage(f"{name}: FAILED - {err}")

    def _on_finalize_cancelled(self, name: str) -> None:
        self.processing_panel.finish_by_name(name, "Cancelled")
        self.statusBar().showMessage(f"{name}: cancelled")

    def _on_finalize_done(self, final_dir: str, rows) -> None:
        self.context.on_finalize_done(final_dir, rows)
        for row in rows:
            self.processing_panel.finish_by_name(row.chosen_name, "archived")
        self.statusBar().showMessage(f"Archived: {final_dir}")

    def closeEvent(self, ev) -> None:
        # Let in-flight compression finish before exit so nothing is left partial.
        self.statusBar().showMessage("Waiting for background compression to finish…")
        self.finalize_pool.wait(30000)
        super().closeEvent(ev)
