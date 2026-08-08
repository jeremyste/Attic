"""The log view should jump to its most recent lines whenever a tab becomes
visible again (e.g. switching tabs), rather than keeping whatever scroll
position it happened to have."""

from __future__ import annotations

import pytest

from attic.core.config import MediaType


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_log_scrolls_to_bottom_when_tab_is_shown(qapp):
    from attic.ui.base_tab import PipelineTab

    tab = PipelineTab(context=None, media_type=MediaType.FLOPPY)
    tab._install_common()
    tab.resize(300, 150)  # give the view a real viewport to scroll within
    for i in range(200):
        tab.append_log(f"line {i}")

    scrollbar = tab.log_view.verticalScrollBar()
    assert scrollbar.maximum() > 0  # sanity: there's actually something to scroll
    scrollbar.setValue(0)  # simulate landing somewhere other than the bottom
    assert scrollbar.value() != scrollbar.maximum()

    tab.show()

    assert scrollbar.value() == scrollbar.maximum()
    tab.hide()
