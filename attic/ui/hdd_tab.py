"""HDD pipeline tab.

Default flow (photo-first, matching photo -> label -> dock -> select): capture
front+back photos and the physical label for the whole drive -> dock the drive
and pick it in the device-selection dialog -> confirmation restating
path/model/size -> first ddrescue pass -> summary with an explicit "Run another
pass" / "Accept and continue" choice -> on accept, partition extraction ->
naming + finalize. A legacy device-first order is available via a setting.

Only removable/USB whole disks are listed by default (core.devices enforces the
hard internal/boot-disk filter); an explicit override can surface all disks
behind warnings.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
)

from ..controllers.base import JobRequest
from ..controllers.hdd import HddExtractWorker, HddRescueWorker
from ..core import devices, staging
from ..core.config import MediaType
from ..core.ddrescue import MapSummary
from .app_context import AppContext
from .base_tab import PipelineTab
from .widgets.hdd_archive_panel import HddArchivePanel
from .widgets.rescue_bar import RescueBar


class HddTab(PipelineTab):
    def __init__(self, context: AppContext, parent=None):
        super().__init__(context, MediaType.HDD, parent)
        self._devices: list[devices.BlockDevice] = []
        # Only one rescue at a time (it holds the drive), but extractions run on
        # the captured image and outlive it, so several can overlap.
        self._rescue: HddRescueWorker | None = None
        self._extracts: list[HddExtractWorker] = []
        self._request: JobRequest | None = None
        self._staging = None
        self._image_path = ""
        self._log_path = ""
        self._stderr_path = ""
        self._accepted_bad_bytes = -1  # set when the user accepts a rescue pass

        self.device_combo = QComboBox()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_devices)
        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Device:"))
        dev_row.addWidget(self.device_combo, 1)
        dev_row.addWidget(self.refresh_btn)
        self._layout.addLayout(dev_row)

        # Safety override: by default only removable/USB disks are listed. Enable
        # this only if a genuine target drive mis-reports as internal.
        self.show_all_check = QCheckBox(
            "Show all drives (advanced — includes internal/system disks)"
        )
        self.show_all_check.toggled.connect(self._on_show_all_toggled)
        self._layout.addWidget(self.show_all_check)

        self.bar = RescueBar()
        self._layout.addWidget(QLabel("Rescue progress:"))
        self._layout.addWidget(self.bar)

        self.archive_panel = HddArchivePanel(context.session.working_folder)
        self.archive_panel.list.setMaximumHeight(220)
        self._layout.addWidget(self.archive_panel)
        # Bound methods of a QWidget get Qt's cross-thread auto-queuing, so
        # this is safe even though the pool emits from a worker thread --
        # unlike a plain lambda, which would run (and touch widgets) there.
        context.finalize_pool.signals.done.connect(self._refresh_archive_panel)
        context.finalize_pool.signals.cancelled.connect(self._refresh_archive_panel)

        self._install_common()

        self.begin_btn.clicked.connect(self._begin)
        self.refresh_devices()

    # --- device listing -----------------------------------------------------

    def _on_show_all_toggled(self, checked: bool) -> None:
        if checked:
            QMessageBox.warning(
                self, "Show all drives",
                "You are enabling ALL drives, including this computer's own "
                "internal and system disks.\n\nImaging the wrong drive can make "
                "your system unbootable. Non-removable drives are marked with ⚠ "
                "and require an extra confirmation.",
            )
        self.refresh_devices()

    def refresh_devices(self) -> None:
        if self.show_all_check.isChecked():
            self._devices = devices.list_all_devices()
        else:
            self._devices = devices.list_removable_devices()
        self.device_combo.clear()
        for d in self._devices:
            self.device_combo.addItem(d.label, d)
        if not self._devices:
            self.device_combo.addItem("No removable/USB drives detected", None)

    def _selected_device(self) -> devices.BlockDevice | None:
        return self.device_combo.currentData()

    # --- capture flow -------------------------------------------------------

    def _begin(self) -> None:
        if self.context.settings.hdd_photo_before_dock:
            device, outcome = self._begin_photo_first()
        else:
            device, outcome = self._begin_device_first()
        if device is None or outcome is None:
            return

        if not self._confirm_device(device):
            return

        self._request = JobRequest(
            working_folder=self.context.session.working_folder,
            media_type=MediaType.HDD,
            physical_label=outcome.physical_label,
            source_id=device.path,
            photos=outcome.photos,
        )
        self._staging = staging.create_staging(
            self._request.working_folder, MediaType.HDD, self._request.session_id
        )
        self._image_path = self._staging.child("drive.img")
        self._log_path = self._staging.child("drive.log")
        self._stderr_path = self._staging.child("ddrescue.stderr")

        panel = self.context.processing_panel
        if panel is not None:
            panel.start_job(
                self._request.session_id, MediaType.HDD,
                outcome.physical_label or device.path,
            )

        self.bar.clear()
        self.set_busy(True)
        self._start_pass(device.path, first_pass=True)

    def _begin_photo_first(self):
        """Workflow: photograph + label the drive, THEN dock and select it."""
        outcome = self.prompt_physical_label()
        if outcome is None:
            return None, None
        from .device_dialog import DeviceSelectionDialog

        dlg = DeviceSelectionDialog(self, show_all=self.show_all_check.isChecked())
        if not dlg.exec():
            return None, None
        device = dlg.selected_device()
        if device is None:
            QMessageBox.warning(self, "No device", "No drive was selected.")
            return None, None
        return device, outcome

    def _begin_device_first(self):
        """Legacy order: pick the already-docked drive, then photograph + label."""
        device = self._selected_device()
        if device is None:
            QMessageBox.warning(self, "No device", "Select a removable/USB drive first.")
            return None, None
        outcome = self.prompt_physical_label()
        if outcome is None:
            return None, None
        return device, outcome

    def _confirm_device(self, device: devices.BlockDevice) -> bool:
        """Confirm the target drive, with an extra hard gate for unsafe drives."""
        details = f"{device.model}\n{device.size}\n{device.path}"
        if device.eligible:
            reply = QMessageBox.question(
                self, "Confirm drive",
                f"Image this drive?\n\n{details}\n\n"
                "This reads the entire device. Make sure it is the correct one.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            return reply == QMessageBox.StandardButton.Yes

        # Non-eligible (internal / system-mounted) — surfaced only via the
        # override. Require an explicit, deliberate second confirmation.
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("⚠ Unsafe drive selected")
        box.setText(
            f"This drive {device.warning}.\n\n{details}\n\n"
            "Imaging the wrong drive can make this computer unbootable or read its "
            "system disk. Only continue if you are certain this is an external "
            "target drive that mis-reports itself.\n\nProceed anyway?"
        )
        proceed = box.addButton("Proceed anyway", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(box.buttons()[-1])
        box.exec()
        return box.clickedButton() is proceed

    def _start_pass(self, device_path: str, *, first_pass: bool) -> None:
        self.set_stage("Rescuing…")
        worker = HddRescueWorker(
            device_path, self._image_path, self._log_path, self._stderr_path,
            first_pass=first_pass, retries=self.context.settings.ddrescue_retries,
        )
        worker.map_progress.connect(self.bar.set_summary)
        worker.stage.connect(self.set_stage)
        worker.log.connect(self.append_log)
        worker.pass_done.connect(lambda s: self._on_pass_done(device_path, s))
        worker.failed.connect(self._on_failed)
        panel = self.context.processing_panel
        if panel is not None and self._request is not None:
            jid = self._request.session_id
            worker.stage.connect(lambda t: panel.set_stage(jid, t))
            # ddrescue's rescued fraction is a genuine measurement, so this bar
            # is determinate rather than a spinner.
            worker.map_progress.connect(
                lambda s: panel.set_percent(jid, int(s.rescued_fraction * 100))
            )
        self._rescue = worker
        worker.start()

    def _on_pass_done(self, device_path: str, summary: MapSummary | None) -> None:
        bad = summary.bad_bytes if summary else 0
        nontried = summary.nontried_bytes if summary else 0
        msg = (
            f"Pass complete.\n\nBad-sector bytes: {bad:,}\n"
            f"Non-tried bytes: {nontried:,}\n\nRun another rescue pass, or accept?"
        )
        box = QMessageBox(self)
        box.setWindowTitle("Rescue pass complete")
        box.setText(msg)
        again = box.addButton("Run another pass", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Accept and continue", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is again:
            self._start_pass(device_path, first_pass=False)
        else:
            self._accepted_bad_bytes = bad
            self._start_extract()

    def _start_extract(self) -> None:
        self.set_stage("Extracting partitions…")
        # Bind this job's identity to the worker rather than reading tab state
        # later: the drive is released here, so a *new* capture may overwrite
        # self._request/_staging/_accepted_bad_bytes while this extraction is
        # still running.
        request, staging_dir = self._request, self._staging
        bad_bytes = self._accepted_bad_bytes
        panel = self.context.processing_panel
        jid = request.session_id

        worker = HddExtractWorker(
            request, staging_dir, self._image_path, self._log_path
        )
        worker.stage.connect(self.set_stage)
        worker.log.connect(self.append_log)
        worker.done.connect(
            lambda result: self._on_extract_done(request, staging_dir, bad_bytes, result)
        )
        worker.failed.connect(self._on_failed)
        if panel is not None:
            worker.stage.connect(lambda t: panel.set_stage(jid, t))
        worker.finished.connect(lambda w=worker: self._forget(w))
        self._extracts.append(worker)
        worker.start()

        # The drive is done: passes are finished and everything from here reads
        # the captured image, so the next drive can be docked now.
        self.set_busy(False)

    def _forget(self, worker) -> None:
        if worker in self._extracts:
            self._extracts.remove(worker)

    def _on_extract_done(self, request, staging_dir, bad_bytes, result) -> None:
        result.read_bad_bytes = bad_bytes
        self.context.route_hdd(request, staging_dir, result)
        self._settle_stage("Idle - ready for next drive")

    def _refresh_archive_panel(self, *_args) -> None:
        self.archive_panel.refresh()

    def _on_failed(self, summary: str) -> None:
        self.append_log(f"FAILED: {summary}")
        self._settle_stage("Failed - see log")
        self.set_busy(False)

    def _settle_stage(self, text: str) -> None:
        """Don't let a finishing job overwrite a newer capture's live status."""
        if not any(w.isRunning() for w in self._extracts):
            self.set_stage(text)
