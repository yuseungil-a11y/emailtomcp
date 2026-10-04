from __future__ import annotations

import pytest

from emailtomcp.app import _load_stylesheet


@pytest.mark.ui
def test_light_theme_qss_loads() -> None:
    css = _load_stylesheet("light")
    assert css
    assert "font-family" in css


@pytest.mark.ui
def test_dark_theme_qss_loads() -> None:
    css = _load_stylesheet("dark")
    assert css
    assert "font-family" in css


@pytest.mark.ui
def test_unknown_theme_returns_empty_string_without_raising() -> None:
    css = _load_stylesheet("does-not-exist")
    assert css == ""
