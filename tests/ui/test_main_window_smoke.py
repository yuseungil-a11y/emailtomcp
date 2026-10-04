from __future__ import annotations

import pytest

from emailtomcp.ui.main_window import MainWindow


@pytest.mark.ui
def test_main_window_opens_and_closes(qtbot) -> None:
    window = MainWindow(version="1.0.0")
    qtbot.addWidget(window)

    window.show()
    assert window.isVisible()
    assert window.windowTitle() == "EmailToMCP"

    window.close()
    assert not window.isVisible()


@pytest.mark.ui
def test_main_window_status_bar_shows_version(qtbot) -> None:
    window = MainWindow(version="1.2.3")
    qtbot.addWidget(window)

    assert "1.2.3" in window._version_label.text()
