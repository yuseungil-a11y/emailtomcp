"""P3 Phase B 화면: 규칙 설정(§11.4), Claude/MCP 패널 잡 탭(§11.5), 계정 자동회신 탭(§11.3).

- 즉시발송(auto_send)은 동작 목록에 보이지만 비활성 + 툴팁(숨기지 않음).
- 전역 토글 off 배너(규칙 목록 위, 드라이런 표 위).
- 드라이런 표·안내 라벨은 평문(비신뢰 제목이 리치텍스트로 해석되지 않음).
- 잡 탭: off면 [일시정지]/[재개] 비활성 + 안내.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QLabel

from emailtomcp.core.errors import PolicyError
from emailtomcp.ui.dialogs import rule_editor_dialog as red
from emailtomcp.ui.dialogs.account_dialog import AccountEditDialog
from emailtomcp.ui.dialogs.mcp_panel import JOBS_OFF_NOTICE, McpPanelDialog
from emailtomcp.ui.dialogs.rule_editor_dialog import (
    AUTOSEND_DISABLED_TOOLTIP,
    RuleEditDialog,
    RuleManagerDialog,
)
from emailtomcp.ui.main_window import MainWindow

pytestmark = pytest.mark.ui

EVIL_SUBJECT = '<img src="file://evil/share/a.png">견적'


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


@dataclass
class _Job:
    id: int
    kind: str = "auto_reply"
    status: str = "done"
    outcome: str | None = "drafted"
    planned_action: str = "draft"
    downgrade_reason: str | None = "autosend_not_yet_supported"
    error: str | None = None
    created_at: str = "2026-10-05T10:00:00+00:00"
    finished_at: str | None = None
    message_id: int = 1
    subject: str | None = EVIL_SUBJECT
    from_addr: str | None = "kim@client.example"
    rule_name: str | None = "견적"
    result_draft_id: int | None = 3


class _Api:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.calls: list[tuple] = []
        self.rules = [
            {
                "id": 1,
                "name": "견적",
                "account_id": None,
                "enabled": True,
                "priority": 10,
                "conditions": {
                    "match": "all",
                    "conditions": [
                        {"field": "from_domain", "op": "in_list", "value": ["client.example"]}
                    ],
                },
                "action": "draft",
                "reply_mode": "generated",
                "fixed_template": None,
                "extra_instructions": None,
                "max_chars": 400,
                "allow_thread_context": False,
                "accept_aligned_dkim": False,
                "cooldown_hours": None,
                "version": 3,
            },
            {
                "id": 2,
                "name": "옛 즉시발송",
                "account_id": None,
                "enabled": True,
                "priority": 20,
                "conditions": {"match": "all", "conditions": []},
                "action": "auto_send",
                "reply_mode": "generated",
                "fixed_template": None,
                "extra_instructions": None,
                "max_chars": 400,
                "allow_thread_context": False,
                "accept_aligned_dkim": False,
                "cooldown_hours": None,
                "version": 1,
            },
        ]
        self.fail_save: Exception | None = None
        self.unmatched = "ignore"

    async def load_tree(self):
        return []

    async def get_setting(self, key, default=None):  # noqa: ANN001
        return default

    async def get_autoreply_toggle(self):
        return {"enabled": self.enabled, "generation": 2, "preflight": {}}

    async def list_rules(self):
        return self.rules

    async def list_accounts(self):
        return [SimpleNamespace(id=1, display_name="업무", email_address="me@example.com")]

    async def get_unmatched_action(self):
        return self.unmatched

    async def set_unmatched_action(self, value):  # noqa: ANN001
        self.calls.append(("unmatched", value))

    async def save_rule(self, rule_id, data, expected_version=None):  # noqa: ANN001
        self.calls.append(("save", rule_id, data, expected_version))
        if self.fail_save:
            raise self.fail_save
        return {"id": rule_id or 9, "version": 1, "warnings": []}

    async def dry_run_rule(self, data, limit=50):  # noqa: ANN001
        self.calls.append(("dry", data, limit))
        return {
            "total": 2,
            "matched": 1,
            "blocked": 1,
            "would_draft": 1,
            "rows": [
                {
                    "message_id": 1,
                    "received_at": "2026-10-05T01:00:00+00:00",
                    "from_addr": "kim@client.example",
                    "subject": EVIL_SUBJECT,
                    "matched": True,
                    "loop_reasons": [],
                    "result": "draft",
                    "note": None,
                },
                {
                    "message_id": 2,
                    "received_at": "2026-10-05T02:00:00+00:00",
                    "from_addr": "news@list.example",
                    "subject": "뉴스",
                    "matched": False,
                    "loop_reasons": ["loop_list_headers"],
                    "result": "blocked",
                    "note": None,
                },
            ],
        }

    async def set_rule_enabled(self, rule_id, enabled):  # noqa: ANN001
        self.calls.append(("enabled", rule_id, enabled))

    async def reorder_rules(self, ids):  # noqa: ANN001
        self.calls.append(("reorder", ids))

    async def delete_rule(self, rule_id):  # noqa: ANN001
        self.calls.append(("delete", rule_id))

    # MCP 패널
    async def mcp_status(self):
        return {"running": True, "port": 8765, "client_connected": False, "stdio_command": "x"}

    async def list_mcp_tokens(self):
        return []

    async def list_mcp_audit(self, limit=200):  # noqa: ANN001
        return []

    async def get_autoreply_queue_status(self):
        return {
            "enabled": self.enabled,
            "queue_state": "running" if self.enabled else None,
            "autosend_state": None,
            "counts": {"done": 1},
            "cli": {"pinned": False, "program": None, "script": None, "problem": "x"},
        }

    async def list_autoreply_jobs(self, limit=100):  # noqa: ANN001
        return [_Job(id=5)]

    async def get_account(self, account_id):  # noqa: ANN001
        return None


@pytest.fixture(autouse=True)
def _no_modal_boxes(monkeypatch):  # noqa: ANN001, ANN202
    """모달 메시지 상자가 테스트를 멈추지 않게 기록만 한다."""
    shown: list[str] = []
    monkeypatch.setattr(red, "plain_warning", lambda _p, _t, text: shown.append(text))
    monkeypatch.setattr(red, "plain_information", lambda _p, _t, text: shown.append(text))
    return shown


def _editor(qtbot, api: _Api, rule=None, *, enabled=False) -> RuleEditDialog:  # noqa: ANN001
    dialog = RuleEditDialog(
        api=api, bridge=_Bridge(), rule=rule, accounts=[(1, "업무")], autoreply_enabled=enabled
    )
    qtbot.addWidget(dialog)
    if rule is None:  # 새 규칙의 기본 조건(보낸사람 도메인 목록) 값을 채운다
        dialog.cond_table.cellWidget(0, 2).setText("client.example")
    return dialog


# ---------------------------------------------------------------- 편집 창


def test_auto_send_choice_is_visible_but_disabled_with_tooltip(qtbot) -> None:  # noqa: ANN001
    dialog = _editor(qtbot, _Api())
    combo = dialog.action_combo
    values = [combo.itemData(i) for i in range(combo.count())]
    assert values == ["ignore", "draft", "auto_send"]
    model = combo.model()
    assert isinstance(model, QStandardItemModel)
    auto_item = model.item(values.index("auto_send"))
    assert not auto_item.isEnabled()
    assert auto_item.toolTip() == AUTOSEND_DISABLED_TOOLTIP
    assert combo.currentData() == "draft"
    assert "다음 업데이트" in dialog.autosend_notice.text()


def test_existing_auto_send_rule_loads_as_draft_with_notice(qtbot) -> None:  # noqa: ANN001
    api = _Api()
    dialog = _editor(qtbot, api, api.rules[1])
    assert dialog.action_combo.currentData() == "draft"
    assert "초안으로 동작" in dialog.autosend_notice.text()


def test_save_sends_values_and_version(qtbot) -> None:  # noqa: ANN001
    api = _Api()
    dialog = _editor(qtbot, api, api.rules[0])
    dialog.name_edit.setText("견적(수정)")
    dialog._on_save()
    kind, rule_id, data, version = api.calls[-1]
    assert (kind, rule_id, version) == ("save", 1, 3)
    assert data["action"] == "draft" and data["name"] == "견적(수정)"
    assert data["conditions"] == {
        "match": "all",
        "conditions": [{"field": "from_domain", "op": "in_list", "value": ["client.example"]}],
    }
    assert dialog.result() == 1


def test_save_policy_error_is_plain_warning_and_keeps_dialog(qtbot, _no_modal_boxes) -> None:  # noqa: ANN001
    api = _Api()
    api.fail_save = PolicyError("정규식 문법 오류")
    warnings = _no_modal_boxes
    dialog = _editor(qtbot, api)
    dialog.name_edit.setText("x")
    dialog._on_save()
    assert warnings and "정규식 문법 오류" in warnings[0]
    assert dialog.result() == 0


def test_empty_condition_value_warns_without_saving(qtbot, _no_modal_boxes) -> None:  # noqa: ANN001
    api = _Api()
    dialog = _editor(qtbot, api)
    dialog.cond_table.cellWidget(0, 2).setText("")
    dialog.name_edit.setText("x")
    dialog._on_save()
    assert _no_modal_boxes and "목록 값" in _no_modal_boxes[0]
    assert not any(c[0] == "save" for c in api.calls)


def test_condition_value_parsing() -> None:
    assert red.text_to_value("from_domain", "in_list", "a.com, b.com\nc.com") == [
        "a.com",
        "b.com",
        "c.com",
    ]
    assert red.text_to_value("received_hour", "between", "18-9") == [18, 9]
    assert red.text_to_value("has_attachment", "equals", "예") is True
    with pytest.raises(ValueError):
        red.text_to_value("received_hour", "between", "아침")
    assert red.value_to_text("from_domain", "in_list", ["a", "b"]) == "a, b"


def test_dry_run_table_is_plain_and_banner_when_off(qtbot) -> None:  # noqa: ANN001
    api = _Api(enabled=False)
    dialog = _editor(qtbot, api)
    dialog.name_edit.setText("x")
    dialog.run_dry_run()
    assert api.calls[-1][0] == "dry"
    assert dialog.dry_banner.isVisibleTo(dialog)
    assert dialog.dry_table.rowCount() == 2
    assert dialog.dry_table.item(0, 2).text() == EVIL_SUBJECT  # 원문 그대로(평문 셀)
    assert "LoopGuard 차단 1건" in dialog.dry_summary.text()
    assert "loop_list_headers" in dialog.dry_table.item(1, 4).text()
    for label in dialog.findChildren(QLabel):
        if label.text() and label.wordWrap():
            assert label.textFormat() == Qt.TextFormat.PlainText


def test_dry_run_banner_hidden_when_on(qtbot) -> None:  # noqa: ANN001
    dialog = _editor(qtbot, _Api(enabled=True), enabled=True)
    assert not dialog.dry_banner.isVisibleTo(dialog)


# ---------------------------------------------------------------- 목록 창


def test_manager_banner_and_rows(qtbot) -> None:  # noqa: ANN001
    api = _Api(enabled=False)
    dialog = RuleManagerDialog(api=api, bridge=_Bridge())
    qtbot.addWidget(dialog)
    assert dialog.banner.isVisibleTo(dialog) and dialog.enable_button.isVisibleTo(dialog)
    assert dialog.banner.textFormat() == Qt.TextFormat.PlainText
    assert "규칙은 저장되지만 적용되지 않습니다" in dialog.banner.text()
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(1, 4).text() == "즉시발송(초안으로 동작)"
    assert dialog.unmatched_ignore.isChecked()
    dialog.unmatched_draft.setChecked(True)
    dialog._on_close()
    assert ("unmatched", "draft") in api.calls


def test_manager_banner_hidden_when_on_and_reorder(qtbot) -> None:  # noqa: ANN001
    api = _Api(enabled=True)
    dialog = RuleManagerDialog(api=api, bridge=_Bridge())
    qtbot.addWidget(dialog)
    assert not dialog.banner.isVisibleTo(dialog)
    dialog.table.selectRow(1)
    dialog._on_move(-1)
    assert ("reorder", [2, 1]) in api.calls


def test_main_window_rules_menu_opens_manager(qtbot, monkeypatch) -> None:  # noqa: ANN001
    opened: list = []
    monkeypatch.setattr(RuleManagerDialog, "exec", lambda self: opened.append(self) or 0)
    window = MainWindow(version="1.0.0", ui_api=_Api(), bridge=_Bridge())
    qtbot.addWidget(window)
    window.act_open_rules.trigger()
    assert len(opened) == 1


# ---------------------------------------------------------------- Claude/MCP 패널 잡 탭


def test_jobs_tab_disables_queue_buttons_when_off(qtbot) -> None:  # noqa: ANN001
    panel = McpPanelDialog(api=_Api(enabled=False), bridge=_Bridge())
    qtbot.addWidget(panel)
    assert panel.tabs.tabText(panel.jobs_tab_index) == "잡"
    assert not panel.pause_button.isEnabled() and not panel.resume_button.isEnabled()
    assert panel.pause_button.toolTip() == JOBS_OFF_NOTICE
    assert not panel.jobs_off_label.isHidden()
    assert panel.jobs_table.rowCount() == 1
    assert panel.jobs_table.item(0, 3).text() == EVIL_SUBJECT
    assert panel.jobs_table.item(0, 6).text() == "초안 생성"
    assert "즉시발송 미지원" in panel.jobs_table.item(0, 7).text()
    assert "미고정" in panel.cli_status_label.text()
    assert panel.cli_status_label.textFormat() == Qt.TextFormat.PlainText


def test_jobs_tab_enables_pause_when_on(qtbot) -> None:  # noqa: ANN001
    panel = McpPanelDialog(api=_Api(enabled=True), bridge=_Bridge())
    qtbot.addWidget(panel)
    assert panel.pause_button.isEnabled() and not panel.resume_button.isEnabled()
    assert panel.jobs_off_label.isHidden()


# ---------------------------------------------------------------- 계정 자동회신 탭


def test_account_autoreply_tab_trust_fields_disabled_until_measured(qtbot) -> None:  # noqa: ANN001
    """데카르트 31번 M-4(§7.6 ③): ar_capability(R17 실측)가 없으니 신뢰 AR 입력란은 비활성이고
    안내만 보인다. 저장 필드에도 두 값을 싣지 않는다(기존 DB 값을 바꾸지 않음)."""
    from emailtomcp.ui.dialogs import account_dialog

    dialog = AccountEditDialog(account_id=None, api=_Api(enabled=False), bridge=_Bridge())
    qtbot.addWidget(dialog)
    assert not dialog._global_off_label.isHidden()  # 전역 off 안내(체크박스는 편집 가능)
    assert dialog._auto_reply_enabled.isEnabled()
    assert not dialog._trusted_authserv_id.isEnabled()
    assert not dialog._trusted_boundary_by.isEnabled()
    assert "다음 업데이트에서 실측 완료 후 제공됩니다" in account_dialog.TRUST_FIELDS_NOTE
    notes = [
        w.text()
        for w in dialog.findChildren(QLabel)
        if w.text() == account_dialog.TRUST_FIELDS_NOTE
    ]
    assert len(notes) == 1
    dialog._auto_reply_enabled.setChecked(True)
    dialog._trusted_authserv_id.setText("mx.example.com")  # 프로그램으로 넣어도 저장 안 됨
    fields = dialog._collect_fields()
    assert fields["auto_reply_enabled"] is True
    assert "trusted_authserv_id" not in fields and "trusted_boundary_by" not in fields


# ---------------------------------------------------------------- 잡 탭 [초안 열기](M-3)


def test_jobs_tab_open_draft_opens_compose_with_guard_banner(qtbot, monkeypatch) -> None:  # noqa: ANN001
    from emailtomcp.ui.dialogs import mcp_panel

    opened: list = []

    class _Draft:
        id = 3
        account_id = 1
        status = "draft"

    api = _Api(enabled=True)

    async def get_draft(draft_id):  # noqa: ANN001, ANN202
        return _Draft()

    api.get_draft = get_draft  # type: ignore[attr-defined]

    def fake_open(account_id, draft_id, **kw):  # noqa: ANN001, ANN202
        opened.append((account_id, draft_id))
        return SimpleNamespace(show=lambda: None)

    monkeypatch.setattr(mcp_panel.ComposeWindow, "open_existing", staticmethod(fake_open))
    panel = McpPanelDialog(api=api, bridge=_Bridge())
    qtbot.addWidget(panel)
    assert not panel.open_draft_button.isEnabled()  # 선택 전
    panel.jobs_table.selectRow(0)
    assert panel.open_draft_button.isEnabled()
    panel.open_draft_button.click()
    assert opened == [(1, 3)]
