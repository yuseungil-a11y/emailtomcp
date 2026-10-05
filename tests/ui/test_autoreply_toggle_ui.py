"""P3 Phase A 화면: 설정 > 자동회신 탭 전역 토글, 켜기 확인창, 메인 창 상태줄 (§11.1, §11.7).

- 켜기 확인창: 기본 버튼 [취소], 1.5초 입력잠금(잠금 중 Enter 무시), 문구는 PlainText.
- off → on 저장: 확인 후 탭을 열 때 읽은 세대로 enable_autoreply(CAS). 취소면 호출하지 않는다.
  CAS 실패면 평문 경고 후 다시 읽고 창을 닫지 않는다.
- on → off 저장: 확인창 없이 disable_autoreply, 결과 정보창(0건이어도 표시).
- 상태줄: `자동회신 ○꺼짐`/`자동회신 ●켜짐`, 클릭하면 설정 > 자동회신 탭,
  off면 긴급정지 비활성+툴팁.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from emailtomcp.core.errors import PolicyError
from emailtomcp.ui.dialogs import settings_dialog
from emailtomcp.ui.dialogs.send_approval_dialog import INPUT_LOCK_MS
from emailtomcp.ui.dialogs.settings_dialog import (
    TAB_AUTOREPLY,
    AutoReplyEnableConfirmDialog,
    SettingsDialog,
)
from emailtomcp.ui.main_window import KILL_SWITCH_TOOLTIP_OFF, MainWindow

pytestmark = pytest.mark.ui


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
    def __init__(self, *, enabled: bool = False, generation: int = 4) -> None:
        self.enabled = enabled
        self.generation = generation
        self.calls: list[tuple] = []
        self.toggle_reads = 0
        self.fail_enable: Exception | None = None

    async def load_tree(self):
        return []

    async def get_setting(self, key, default=None):  # noqa: ANN001
        return default

    async def set_setting(self, key, value):  # noqa: ANN001
        self.calls.append(("set_setting", key, value))

    async def get_autoreply_toggle(self):
        self.toggle_reads += 1
        return {
            "enabled": self.enabled,
            "generation": self.generation,
            "changed_at": "2026-10-05T05:02:00+00:00",
            "watermark_message_id": 0,
            "preflight": {
                "autoreply_accounts": 1,
                "rules_total": 3,
                "rules_enabled": 2,
                "rules_auto_send": 1,
                "accounts_without_trust": 1,
                "unmatched_action": "ignore",
                "claude_cli_pinned": None,
            },
        }

    async def enable_autoreply(self, expected_generation):  # noqa: ANN001
        self.calls.append(("enable", expected_generation))
        if self.fail_enable is not None:
            self.generation += 1  # 다른 곳에서 바뀐 상황
            raise self.fail_enable
        self.enabled = True
        self.generation += 1
        return {"enabled": True, "generation": self.generation}

    async def disable_autoreply(self):
        self.calls.append(("disable",))
        self.enabled = False
        self.generation += 1
        return {
            "enabled": False,
            "generation": self.generation,
            "cancelled_jobs": 0,
            "reverted_outbox": 0,
            "sending_in_flight": 0,
        }


def _settings(qtbot, api: _Api, **kw) -> SettingsDialog:  # noqa: ANN001, ANN003
    dialog = SettingsDialog(api=api, bridge=_Bridge(), apply_theme=lambda _p: None, **kw)
    qtbot.addWidget(dialog)
    return dialog


# ---------------------------------------------------------------- 켜기 확인창


def test_enable_confirm_dialog_locks_input_with_cancel_default(qtbot) -> None:  # noqa: ANN001
    dialog = AutoReplyEnableConfirmDialog({"autoreply_accounts": 2, "rules_total": 0})
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.is_input_locked
    assert dialog.cancel_button.isDefault()
    assert not dialog.enable_button.isDefault()
    assert not dialog.enable_button.isEnabled()

    qtbot.keyClick(dialog, Qt.Key.Key_Return)  # 잠금 중 Enter는 무시
    assert dialog.isVisible()
    assert dialog.result() == 0

    qtbot.waitUntil(lambda: not dialog.is_input_locked, timeout=INPUT_LOCK_MS + 2000)
    assert dialog.enable_button.isEnabled()
    assert dialog.cancel_button.isEnabled()


def test_enable_confirm_dialog_lock_lasts_about_input_lock_ms(qtbot) -> None:  # noqa: ANN001
    dialog = AutoReplyEnableConfirmDialog(None)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(INPUT_LOCK_MS // 2)
    assert dialog.is_input_locked
    dialog._on_enable_clicked()  # 잠금 중 클릭 경로도 무시
    assert dialog.result() == 0


def test_enable_confirm_dialog_labels_are_plain_text(qtbot) -> None:  # noqa: ANN001
    dialog = AutoReplyEnableConfirmDialog({"autoreply_accounts": 1})
    qtbot.addWidget(dialog)
    for label in dialog.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText
    assert "해당없음" in dialog.preflight_label.text()  # claude CLI 항목(Phase B)


# ---------------------------------------------------------------- 설정 > 자동회신 탭


def test_settings_autoreply_tab_shows_state_and_opens_on_request(qtbot) -> None:  # noqa: ANN001
    dialog = _settings(qtbot, _Api(), initial_tab=TAB_AUTOREPLY)
    assert dialog._tabs.currentIndex() == dialog._autoreply_tab_index
    assert dialog._autoreply_enabled.isEnabled()
    assert not dialog._autoreply_enabled.isChecked()
    text = dialog._autoreply_state_label.text()
    assert text.startswith("상태: ○ 꺼짐 — 규칙 3개 보존됨")
    assert dialog._autoreply_state_label.textFormat() == Qt.TextFormat.PlainText


def test_save_off_to_on_confirms_then_enables_with_loaded_generation(qtbot, monkeypatch) -> None:  # noqa: ANN001
    api = _Api(generation=4)
    dialog = _settings(qtbot, api)
    monkeypatch.setattr(dialog, "_confirm_enable", lambda _p: True)
    dialog._autoreply_enabled.setChecked(True)
    dialog._on_save()
    assert ("enable", 4) in api.calls
    assert dialog.result() == 1  # Accepted
    # 범용 set_setting 루프에는 토글 키가 없다(보호 키)
    assert not any(c[0] == "set_setting" and "enabled" in str(c[1]) for c in api.calls)


def test_save_off_to_on_cancelled_does_not_enable(qtbot, monkeypatch) -> None:  # noqa: ANN001
    api = _Api()
    dialog = _settings(qtbot, api)
    monkeypatch.setattr(dialog, "_confirm_enable", lambda _p: False)
    dialog._autoreply_enabled.setChecked(True)
    dialog._on_save()
    assert not any(c[0] == "enable" for c in api.calls)
    assert not dialog._autoreply_enabled.isChecked()
    assert dialog.result() == 0  # 창은 열려 있다


def test_save_off_to_on_cas_failure_warns_and_reloads(qtbot, monkeypatch) -> None:  # noqa: ANN001
    api = _Api(generation=4)
    api.fail_enable = PolicyError("다른 곳에서 바뀌었습니다. 다시 확인하세요")
    dialog = _settings(qtbot, api)
    warnings: list[str] = []
    monkeypatch.setattr(
        settings_dialog, "plain_warning", lambda _p, _t, text: warnings.append(text)
    )
    monkeypatch.setattr(dialog, "_confirm_enable", lambda _p: True)
    reads_before = api.toggle_reads
    dialog._autoreply_enabled.setChecked(True)
    dialog._on_save()
    assert warnings and "다른 곳에서 바뀌었습니다" in warnings[0]
    assert api.toggle_reads == reads_before + 1  # 다시 읽음
    assert dialog._autoreply_view["generation"] == 5
    assert dialog.result() == 0


def test_save_on_to_off_disables_without_confirm_and_reports(qtbot, monkeypatch) -> None:  # noqa: ANN001
    api = _Api(enabled=True)
    dialog = _settings(qtbot, api)
    infos: list[str] = []
    monkeypatch.setattr(
        settings_dialog, "plain_information", lambda _p, _t, text: infos.append(text)
    )

    def _no_confirm(_p):  # noqa: ANN001, ANN202
        raise AssertionError("끄기에는 확인창이 없어야 한다")

    monkeypatch.setattr(dialog, "_confirm_enable", _no_confirm)
    assert dialog._autoreply_enabled.isChecked()
    dialog._autoreply_enabled.setChecked(False)
    dialog._on_save()
    assert ("disable",) in api.calls
    assert infos, "0건이어도 결과 정보창이 떠야 한다"
    assert "실행 중이던 잡 0건 취소" in infos[0]
    assert "발송 대기 0건을 초안으로 되돌림" in infos[0]
    assert dialog.result() == 1


def test_save_without_toggle_change_calls_neither(qtbot) -> None:  # noqa: ANN001
    api = _Api()
    dialog = _settings(qtbot, api)
    dialog._on_save()
    assert not any(c[0] in ("enable", "disable") for c in api.calls)


def test_settings_without_toggle_api_locks_checkbox(qtbot) -> None:  # noqa: ANN001
    class _OldApi:
        async def get_setting(self, key, default=None):  # noqa: ANN001
            return default

    dialog = SettingsDialog(api=_OldApi(), bridge=_Bridge(), apply_theme=lambda _p: None)
    qtbot.addWidget(dialog)
    assert not dialog._autoreply_enabled.isEnabled()
    assert not dialog._autoreply_enabled.isChecked()


# ---------------------------------------------------------------- 메인 창 상태줄


def test_main_window_status_off_disables_kill_switch(qtbot) -> None:  # noqa: ANN001
    window = MainWindow(version="1.0.0", ui_api=_Api(enabled=False), bridge=_Bridge())
    qtbot.addWidget(window)
    label = window._autosend_status_label
    assert label.text() == "자동회신 ○꺼짐"
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert not window.act_kill_switch.isEnabled()
    assert window.act_kill_switch.toolTip() == KILL_SWITCH_TOOLTIP_OFF


def test_main_window_status_on_and_event_refresh(qtbot) -> None:  # noqa: ANN001
    api = _Api(enabled=True)
    window = MainWindow(version="1.0.0", ui_api=api, bridge=_Bridge())
    qtbot.addWidget(window)
    assert window._autosend_status_label.text() == "자동회신 ●켜짐"
    api.enabled = False
    window._on_backend_event("AutoReplyEnabledChanged", {"enabled": True})  # payload는 믿지 않음
    assert window._autosend_status_label.text() == "자동회신 ○꺼짐"
    assert window.act_kill_switch.toolTip() == KILL_SWITCH_TOOLTIP_OFF


def test_main_window_status_click_opens_autoreply_tab(qtbot, monkeypatch) -> None:  # noqa: ANN001
    window = MainWindow(version="1.0.0", ui_api=_Api(), bridge=_Bridge())
    qtbot.addWidget(window)
    opened: list = []
    monkeypatch.setattr(
        window, "_on_open_settings", lambda initial_tab=None: opened.append(initial_tab)
    )
    # 생성자에서 연결한 람다가 호출 시점에 속성을 찾으므로 인스턴스 대체가 그대로 적용된다.
    qtbot.mouseClick(window._autosend_status_label, Qt.MouseButton.LeftButton)
    assert opened == [TAB_AUTOREPLY]
