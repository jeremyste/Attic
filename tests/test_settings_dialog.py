"""SettingsDialog: round-tripping ddrescue's new stop_after/timeout controls."""

from __future__ import annotations

import pytest

from attic.core.settings import AppSettings


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_defaults_round_trip(qapp):
    from attic.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(AppSettings())
    out = dlg.result_settings()

    assert out.ddrescue_stop_after == "full"
    assert out.ddrescue_timeout_minutes == 0
    assert dlg.retries.isEnabled()  # Full -> retry passes still meaningful


def test_loads_a_non_default_stop_after_and_disables_retries(qapp):
    from attic.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(AppSettings(ddrescue_stop_after="scraping", ddrescue_retries=7))

    assert dlg.stop_after.currentData() == "scraping"
    assert not dlg.retries.isEnabled()  # meaningless when not going to Full
    assert dlg.retries.value() == 7  # still shown/saved even while disabled

    out = dlg.result_settings()
    assert out.ddrescue_stop_after == "scraping"
    assert out.ddrescue_retries == 7


def test_switching_back_to_full_reenables_retries(qapp):
    from attic.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(AppSettings(ddrescue_stop_after="copying"))
    assert not dlg.retries.isEnabled()

    idx = dlg.stop_after.findData("full")
    dlg.stop_after.setCurrentIndex(idx)

    assert dlg.retries.isEnabled()
