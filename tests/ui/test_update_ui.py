"""업데이트 UI — §11.1 배너, §11.7 설정 업데이트 탭, §11.8 상태 표."""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QSettings

from emailtomcp.ui.dialogs.settings_dialog import SettingsDialog
from emailtomcp.ui.main_window import MainWindow
from emailtomcp.ui.widgets import update_banner
from emailtomcp.ui.widgets.update_banner import (
    compute_banner,
    describe_update_status,
    is_safe_release_url,
    status_badge,
)

PAGE = "https://github.com/yuseungil-a11y/emailtomcp/releases/tag/v1.0.1"
NOW = datetime(2026, 11, 15, tzinfo=UTC)


def _view(**kw) -> dict:  # noqa: ANN003
    base = {
        "kind": "available",
        "current_version": "1.0.0",
        "latest_version": "1.0.1",
        "security": False,
        "release_page": PAGE,
        "notes_summary": "버그 수정",
        "below_floor": False,
        "floor_version": "1.0.0",
        "stale": False,
        "auto_check": True,
        "checked_at": "2026-11-15T00:00:00+00:00",
    }
    base.update(kw)
    return base


def _banner(view, **kw):  # noqa: ANN001, ANN003, ANN202
    return compute_banner(
        view,
        now=kw.get("now", NOW),
        dismissed_version=kw.get("dismissed_version"),
        security_dismissed_at=kw.get("security_dismissed_at"),
        session_dismissed=kw.get("session_dismissed", set()),
    )


# ---------------------------------------------------------------- 순수 로직(§11.8 표)


def test_up_to_date_has_no_banner_but_settings_text() -> None:
    view = _view(kind="up_to_date")
    assert _banner(view) is None
    assert status_badge(view) is None
    assert describe_update_status(view) == "최신 버전입니다."


def test_available_banner_and_badge() -> None:
    spec = _banner(_view())
    assert spec is not None and spec.level == "available" and spec.dismissible and spec.has_link
    assert status_badge(_view()) == ("업데이트 ●1.0.1", "connecting")


def test_available_dismissed_for_same_version_only() -> None:
    assert _banner(_view(), dismissed_version="1.0.1") is None
    assert _banner(_view(latest_version="1.0.2"), dismissed_version="1.0.1") is not None


def test_security_update_reappears_after_24h() -> None:
    view = _view(security=True)
    assert _banner(view).level == "security"
    assert _banner(view, security_dismissed_at=NOW - timedelta(hours=1)) is None
    assert _banner(view, security_dismissed_at=NOW - timedelta(hours=25)).level == "security"


def test_floor_banner_is_fixed_and_survives_unverifiable() -> None:
    spec = _banner(_view(kind="below_floor", below_floor=True, floor_version="1.0.1"))
    assert spec.level == "floor" and not spec.dismissible
    still = _banner(_view(kind="unverifiable", below_floor=True, reason="네트워크 오류"))
    assert still is not None and still.level == "floor"
    assert status_badge(_view(kind="below_floor", below_floor=True))[1] == "disconnected"


def test_unverifiable_banner_only_after_7_days() -> None:
    assert _banner(_view(kind="unverifiable", stale=False)) is None
    spec = _banner(_view(kind="unverifiable", stale=True))
    assert spec.level == "stale"
    assert _banner(_view(kind="unverifiable", stale=True), session_dismissed={"stale"}) is None
    assert "확인 불가" in describe_update_status(_view(kind="unverifiable", reason="만료"))


def test_security_alert_badge_and_text() -> None:
    view = _view(kind="security_alert", reason="서명 검증 실패", halted=True)
    assert status_badge(view) == ("업데이트 ●보안 경보", "disconnected")
    assert "보안 경보" in describe_update_status(view)


def test_banner_hides_link_buttons_for_unsafe_url() -> None:
    spec = _banner(_view(release_page="https://evil.example.com/x"))
    assert spec is not None and not spec.has_link


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (PAGE, True),
        ("https://github.com/yuseungil-a11y/emailtomcp/releases/download/v1.0.1/a.exe", True),
        ("http://github.com/yuseungil-a11y/emailtomcp/releases/tag/v1.0.1", False),
        ("https://github.com/other/emailtomcp/releases/tag/v1", False),
        ("https://github.com/yuseungil-a11y/emailtomcp/releases/../../x", False),
        ("https://github.com:444/yuseungil-a11y/emailtomcp/releases/tag/v1", False),
        ("https://user@github.com/yuseungil-a11y/emailtomcp/releases/tag/v1", False),
        ("https://github.com/yuseungil-a11y/emailtomcp/releases/tag/v1?x=1", False),
        ("file:///C:/evil", False),
        (None, False),
    ],
)
def test_is_safe_release_url(url, ok) -> None:  # noqa: ANN001
    assert is_safe_release_url(url) is ok


# ---------------------------------------------------------------- 위젯/화면


class _Signal:
    def connect(self, _slot) -> None:  # noqa: ANN001
        pass


class _Bridge:
    event_received = _Signal()

    def call_backend(self, coro, timeout=5.0):  # noqa: ANN001, ARG002
        return asyncio.run(coro)

    def call_backend_async(self, coro):  # noqa: ANN001
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_result(asyncio.run(coro))
        return future


class _Api:
    def __init__(self, view: dict) -> None:
        self.view = view
        self.checked = 0
        self.auto_set: list[bool] = []

    async def load_tree(self):
        return []

    async def get_setting(self, key, default=None):  # noqa: ANN001
        return default

    async def set_setting(self, key, value):  # noqa: ANN001
        return None

    async def get_update_status(self):
        return dict(self.view)

    async def check_update_now(self):
        self.checked += 1
        return dict(self.view)

    async def set_update_auto_check(self, enabled):  # noqa: ANN001
        self.auto_set.append(enabled)


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path, monkeypatch):
    """배너의 [나중에] 기록이 실제 사용자 설정(Windows 레지스트리)에 남지 않게 격리한다.

    `QSettings("UTInfo", "EmailToMCP")`는 네이티브 형식이라 setDefaultFormat의 영향을 받지
    않으므로, 배너 모듈의 QSettings 생성 자체를 임시 ini 파일로 바꿔치기한다.
    """
    ini_path = str(tmp_path / "update_banner_test.ini")
    monkeypatch.setattr(
        update_banner,
        "QSettings",
        lambda *_args: QSettings(ini_path, QSettings.Format.IniFormat),
    )
    yield


@pytest.mark.ui
def test_main_window_shows_banner_and_badge_for_available(qtbot) -> None:
    window = MainWindow(version="1.0.0", ui_api=_Api(_view()), bridge=_Bridge())
    qtbot.addWidget(window)
    window.show()
    assert window._update_banner.isVisible()
    assert "1.0.1" in window._update_banner.text
    assert window._update_status_label.isVisible()


@pytest.mark.ui
def test_main_window_floor_banner_has_no_later_button(qtbot) -> None:
    view = _view(kind="below_floor", below_floor=True, floor_version="1.0.1")
    window = MainWindow(version="1.0.0", ui_api=_Api(view), bridge=_Bridge())
    qtbot.addWidget(window)
    window.show()
    assert window._update_banner.isVisible()
    assert not window._update_banner.btn_later.isVisible()
    assert "필수 업데이트" in window._update_banner.text


@pytest.mark.ui
def test_banner_later_hides_and_download_opens_only_safe_url(qtbot, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        update_banner.QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
    )
    window = MainWindow(version="1.0.0", ui_api=_Api(_view()), bridge=_Bridge())
    qtbot.addWidget(window)
    window.show()
    window._update_banner.btn_download.click()
    assert opened == [PAGE]
    window._update_banner.btn_later.click()
    assert not window._update_banner.isVisible()
    # 같은 버전이면 다시 그려도 숨김 유지
    window.apply_update_status(_view())
    assert not window._update_banner.isVisible()


@pytest.mark.ui
def test_main_window_without_update_api_still_works(qtbot) -> None:
    window = MainWindow(version="1.0.0")
    qtbot.addWidget(window)
    window.show()
    assert not window._update_banner.isVisible()


@pytest.mark.ui
def test_settings_update_tab_shows_status_and_checks_now(qtbot) -> None:
    api = _Api(_view(kind="up_to_date", current_version="1.0.1", latest_version="1.0.1"))
    dialog = SettingsDialog(api=api, bridge=_Bridge(), apply_theme=lambda _p: None)
    qtbot.addWidget(dialog)
    assert dialog._update_current_version.text() == "1.0.1"
    assert dialog._update_result.text() == "최신 버전입니다."
    assert dialog._update_auto_check.isChecked()
    dialog._update_check_now.click()
    qtbot.waitUntil(lambda: api.checked == 1 and dialog._update_check_now.isEnabled(), timeout=3000)
    # 자동 확인을 끄고 저장하면 저장 API가 호출된다
    dialog._update_auto_check.setChecked(False)
    dialog._on_save()
    assert api.auto_set == [False]


@pytest.mark.ui
def test_settings_dev_build_label(qtbot) -> None:
    api = _Api(
        _view(kind="never", current_version="1.0.1.dev3+gabc", is_dev_build=True, auto_check=False)
    )
    dialog = SettingsDialog(api=api, bridge=_Bridge(), apply_theme=lambda _p: None)
    qtbot.addWidget(dialog)
    assert "개발 빌드" in dialog._update_current_version.text()
    assert not dialog._update_auto_check.isChecked()
