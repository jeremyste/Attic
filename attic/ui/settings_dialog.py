"""Settings dialog — edits per-working-folder :class:`AppSettings`.

Values are saved into the working folder (``attic_settings.json``) so they travel
with the archive rather than the machine.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from ..core.settings import AppSettings


class SettingsDialog(QDialog):
    """Edit and return an :class:`AppSettings`."""

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Attic settings")
        self._start = settings

        # Compression
        self.level = QSpinBox()
        self.level.setRange(1, 22)
        self.level.setValue(settings.zstd_level)
        self.long = QCheckBox("Use --long window (better ratio, more memory)")
        self.long.setChecked(settings.zstd_long)
        self.keep_raw = QCheckBox("Keep uncompressed .img alongside the .zst")
        self.keep_raw.setChecked(settings.keep_raw_image)

        comp = QFormLayout()
        comp.addRow("zstd level:", self.level)
        comp.addRow(self.long)
        comp.addRow(self.keep_raw)

        # Devices
        self.optical = QLineEdit(settings.optical_device)
        self.retries = QSpinBox()
        self.retries.setRange(0, 20)
        self.retries.setValue(settings.ddrescue_retries)
        dev = QFormLayout()
        self.eject_on_complete = QCheckBox("Eject the disc when imaging finishes")
        self.eject_on_complete.setChecked(settings.eject_on_complete)
        self.eject_on_complete.setToolTip(
            "A physical cue that it's safe to pull the disc and load the "
            "next one. Best-effort -- never fails the capture."
        )
        dev.addRow("Optical device:", self.optical)
        dev.addRow("ddrescue retries:", self.retries)
        dev.addRow(self.eject_on_complete)

        self.convert_dvd_video = QCheckBox(
            "Auto-convert DVD-Video (VIDEO_TS) discs to .mp4"
        )
        self.convert_dvd_video.setChecked(settings.convert_dvd_video)
        self.convert_dvd_video.setToolTip(
            "After extraction, detect a VIDEO_TS folder and transcode each "
            "title into an ordinary .mp4 alongside it. Needs ffmpeg on PATH; "
            "if it's missing, the raw VIDEO_TS copy is kept either way."
        )
        self.dvd_video_crf = QSpinBox()
        self.dvd_video_crf.setRange(0, 51)
        self.dvd_video_crf.setValue(settings.dvd_video_crf)
        self.dvd_video_crf.setToolTip(
            "x264 quality (CRF): lower = higher quality/larger file. 18 is "
            "visually near-lossless; 23 is a typical 'good enough' default."
        )
        dev.addRow(self.convert_dvd_video)
        dev.addRow("DVD video quality (CRF):", self.dvd_video_crf)

        # Floppy geometry
        self.cyls = QSpinBox()
        self.cyls.setRange(1, 100)
        self.cyls.setValue(settings.floppy_cylinders)
        self.heads = QSpinBox()
        self.heads.setRange(1, 2)
        self.heads.setValue(settings.floppy_heads)
        self.floppy_format = QLineEdit(settings.floppy_format)
        self.floppy_format.setToolTip(
            "gw disk format. 'ibm.scan' probes the IBM FM/MFM variants per track "
            "and suits DOS-era disks; run 'gw read --help' for the full list."
        )
        self.floppy_device = QLineEdit(settings.floppy_device)
        self.floppy_device.setPlaceholderText("auto-detect")
        flop = QFormLayout()
        flop.addRow("Floppy cylinders:", self.cyls)
        flop.addRow("Floppy heads:", self.heads)
        self.capture_flux = QCheckBox(
            "Preserve flux (.scp) and decode the image from it"
        )
        self.capture_flux.setChecked(settings.floppy_capture_flux)
        self.capture_flux.setToolTip(
            "Archives a re-decodable master alongside the image, at roughly "
            "10-15 MB compressed per disk. Also frees the drive sooner: only the "
            "flux read needs the hardware."
        )
        self.flux_revs = QSpinBox()
        self.flux_revs.setRange(0, 10)
        self.flux_revs.setSpecialValueText("format default")
        self.flux_revs.setValue(settings.floppy_flux_revs)
        self.floppy_retries = QSpinBox()
        self.floppy_retries.setRange(0, 50)
        self.floppy_retries.setSpecialValueText("gw default (3)")
        self.floppy_retries.setValue(settings.floppy_retries)
        self.floppy_retries.setToolTip(
            "In-place re-reads of a track before giving up on it. gw stops "
            "retrying the moment a track fully succeeds, so this only costs "
            "time on tracks that are already failing."
        )
        self.floppy_seek_retries = QSpinBox()
        self.floppy_seek_retries.setRange(0, 10)
        self.floppy_seek_retries.setSpecialValueText("gw default (off)")
        self.floppy_seek_retries.setValue(settings.floppy_seek_retries)
        self.floppy_seek_retries.setToolTip(
            "Retract the head and re-seek before each retry, instead of just "
            "re-reading in place -- a genuinely different recovery attempt "
            "(can dislodge dust/debris), not just another chance at the same "
            "read."
        )
        flop.addRow("Disk format:", self.floppy_format)
        flop.addRow("Greaseweazle port:", self.floppy_device)
        flop.addRow(self.capture_flux)
        flop.addRow("Flux revolutions:", self.flux_revs)
        flop.addRow("Track retries:", self.floppy_retries)
        flop.addRow("Seek retries:", self.floppy_seek_retries)

        # Webcam
        self.camera = QSpinBox()
        self.camera.setRange(0, 16)
        self.camera.setValue(settings.camera_index)
        self.skip_photo = QCheckBox("Never prompt for a photo")
        self.skip_photo.setChecked(settings.skip_photo)
        cam = QFormLayout()
        cam.addRow("Camera index:", self.camera)
        cam.addRow(self.skip_photo)

        # Naming / workflow
        self.auto_accept = QCheckBox(
            "Unattended naming (never interrupt a capture to confirm a name)"
        )
        self.auto_accept.setChecked(settings.auto_accept_fallback_names)
        self.auto_accept.setToolTip(
            "Use the label you typed, else a detected volume label, else a "
            "generated name -- with no confirmation dialog. Disks with no label "
            "at all are still listed in Pending Labels to name later."
        )
        self.hdd_photo_first = QCheckBox(
            "HDD: photograph & label before selecting the drive"
        )
        self.hdd_photo_first.setChecked(settings.hdd_photo_before_dock)
        flow = QVBoxLayout()
        flow.addWidget(self.auto_accept)
        flow.addWidget(QLabel(
            "   Nothing modal interrupts a capture, so you can load the next\n"
            "   disk while the last one is still processing. Unlabeled disks\n"
            "   keep a generated name and are not queued in Pending Labels."
        ))
        flow.addWidget(self.hdd_photo_first)

        layout = QVBoxLayout(self)
        layout.addWidget(_group("Compression", comp))
        layout.addWidget(_group("Devices", dev))
        layout.addWidget(_group("Floppy", flop))
        layout.addWidget(_group("Webcam", cam))
        layout.addWidget(_group("Naming & workflow", flow))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_settings(self) -> AppSettings:
        return AppSettings(
            zstd_level=self.level.value(),
            zstd_long=self.long.isChecked(),
            keep_raw_image=self.keep_raw.isChecked(),
            optical_device=self.optical.text().strip() or "/dev/sr0",
            ddrescue_retries=self.retries.value(),
            eject_on_complete=self.eject_on_complete.isChecked(),
            convert_dvd_video=self.convert_dvd_video.isChecked(),
            dvd_video_crf=self.dvd_video_crf.value(),
            floppy_cylinders=self.cyls.value(),
            floppy_heads=self.heads.value(),
            floppy_format=self.floppy_format.text().strip() or "ibm.scan",
            floppy_device=self.floppy_device.text().strip(),
            floppy_capture_flux=self.capture_flux.isChecked(),
            floppy_flux_revs=self.flux_revs.value(),
            floppy_retries=self.floppy_retries.value(),
            floppy_seek_retries=self.floppy_seek_retries.value(),
            camera_index=self.camera.value(),
            skip_photo=self.skip_photo.isChecked(),
            auto_accept_fallback_names=self.auto_accept.isChecked(),
            hdd_photo_before_dock=self.hdd_photo_first.isChecked(),
        )


def _group(title: str, inner) -> QGroupBox:
    box = QGroupBox(title)
    box.setLayout(inner)
    return box
