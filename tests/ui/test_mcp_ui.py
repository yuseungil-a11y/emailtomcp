"""P2 화면 스모크/동작 테스트: 발송 승인 다이얼로그(§11.6, M5), Claude/MCP 패널(§11.5),
메인 창의 MCP 이벤트 반영(§11.1 상태줄, 승인 요청 → 다이얼로그).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from dataclasses import dataclass, field

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox

from emailtomcp.app import ApprovalView
from emailtomcp.mcp_server.approval import ApprovalOutcome
from emailtomcp.mcp_server.auth import IssuedToken
from emailtomcp.storage.repositories.mcp import McpAuditRow, McpTokenRow
from emailtomcp.ui.dialogs.mcp_panel import (
    ConnectInfoDialog,
    McpPanelDialog,
    TokenIssueDialog,
    TokenRevealDialog,
)
from emailtomcp.ui.dialogs.send_approval_dialog import INPUT_LOCK_MS, SendApprovalDialog
from emailtomcp.ui.main_window import MainWindow

pytestmark = pytest.mark.ui


class _FakeSignal:
    def connect(self, _slot) -> None:  # noqa: ANN001
        pass


class _FakeBridge:
    event_received = _FakeSignal()

    def call_backend(self, coro, timeout=5.0):  # noqa: ANN001, ARG002
        return asyncio.run(coro)

    def call_backend_async(self, coro):  # noqa: ANN001
        future: concurrent.futures.Future = concurrent.futures.Future()
        future.set_result(asyncio.run(coro))
        return future


@dataclass
class _Draft:
    id: int = 7
    account_id: int = 1
    kind: str = "reply"
    to_addrs: list[str] = field(default_factory=lambda: ["kim@partner.example"])
    cc_addrs: list[str] = field(default_factory=lambda: ["me2@example.com"])
    bcc_addrs: list[str] = field(default_factory=list)
    subject: str | None = "Re: 견적서"
    body_text: str | None = "회신 본문"
    attachments: list[dict] = field(
        default_factory=lambda: [{"filename": "a.pdf", "size_bytes": 2048}]
    )
    status: str = "pending_approval"


class _FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get_approval_view(self, draft_id):  # noqa: ANN001
        return ApprovalView(
            draft=_Draft(id=draft_id),  # type: ignore[arg-type]
            account_label="테스트 <me@example.com>",
            snapshot_hash="hash-shown",
            external_recipients=["kim@partner.example"],
        )

    async def approve_draft(self, draft_id, expected_hash):  # noqa: ANN001
        self.calls.append(("approve", draft_id, expected_hash))
        return ApprovalOutcome(True, "approved", "승인했습니다.")

    async def reject_draft(self, draft_id):  # noqa: ANN001
        self.calls.append(("reject", draft_id))
        return True

    async def cancel_approval_for_edit(self, draft_id):  # noqa: ANN001
        self.calls.append(("edit", draft_id))
        return True

    async def mcp_status(self):
        return {
            "running": False,
            "port": 8765,
            "error": "포트 8765를 사용할 수 없습니다",
            "client_connected": False,
            "stdio_command": 'claude mcp add emailtomcp --scope user -- "x.exe" --mcp-stdio-proxy',
        }

    async def list_mcp_tokens(self):
        return [
            McpTokenRow(
                1,
                "default",
                "proxy",
                ["read", "draft"],
                "h" * 64,
                "2026-10-01",
                None,
                "2027-03-30",
                None,
            ),
            McpTokenRow(
                2,
                "laptop",
                "http",
                ["read"],
                "g" * 64,
                "2026-10-01",
                "2026-10-02",
                "2027-03-30",
                "2026-10-03",
            ),
        ]

    async def list_mcp_audit(self, limit=200):  # noqa: ANN001
        return [
            McpAuditRow(
                1,
                "2026-10-05T01:00:00+00:00",
                "interactive",
                1,
                "abcd1234",
                None,
                "send_draft",
                "{}",
                "denied",
                403,
                "scope",
            )
        ]

    async def issue_mcp_token(self, **kwargs):  # noqa: ANN003
        # proxy 토큰은 keyring에만 저장되어 평문이 없고(§6.4), http 토큰만 1회 표시된다.
        if kwargs.get("kind") == "proxy":
            return IssuedToken(4, "proxy", None)
        return IssuedToken(3, "http", "plain")

    async def revoke_mcp_token(self, token_id):  # noqa: ANN001
        return True

    async def list_pending_approvals(self):
        return [_Draft()]

    # MainWindow가 기동 시 부르는 메서드(빈 상태)
    async def load_tree(self):
        return []

    async def get_setting(self, key, default=None):  # noqa: ANN001
        return default


def test_approval_dialog_locks_input_then_enables_with_reject_default(qtbot) -> None:  # noqa: ANN001
    api = _FakeApi()
    dialog = SendApprovalDialog(draft_id=7, api=api, bridge=_FakeBridge())
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert dialog.is_input_locked
    assert not dialog.approve_button.isEnabled() and not dialog.reject_button.isEnabled()
    assert dialog.reject_button.isDefault() and not dialog.approve_button.isDefault()
    # 잠긴 동안의 Enter/클릭은 아무 결정도 만들지 않는다.
    qtbot.keyClick(dialog, Qt.Key.Key_Return)
    dialog._on_approve_clicked()
    assert api.calls == []
    qtbot.waitUntil(lambda: not dialog.is_input_locked, timeout=INPUT_LOCK_MS + 2000)
    assert dialog.approve_button.isEnabled() and dialog.reject_button.isEnabled()
    badges = [
        w
        for w in dialog.findChildren(QLabel)
        if w.property("badge") == "external" and w.text() == "외부"
    ]
    assert len(badges) == 1  # kim@partner.example만 외부, me2@example.com은 같은 도메인


def test_approval_dialog_approve_sends_shown_hash(qtbot) -> None:  # noqa: ANN001
    api = _FakeApi()
    dialog = SendApprovalDialog(draft_id=7, api=api, bridge=_FakeBridge())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: not dialog.is_input_locked, timeout=INPUT_LOCK_MS + 2000)
    dialog.approve_button.click()
    assert api.calls == [("approve", 7, "hash-shown")]


def test_approval_dialog_edit_cancels_and_requests_compose(qtbot) -> None:  # noqa: ANN001
    api = _FakeApi()
    opened: list[tuple[int, int]] = []
    dialog = SendApprovalDialog(
        draft_id=7,
        api=api,
        bridge=_FakeBridge(),
        on_edit_requested=lambda a, d: opened.append((a, d)),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: not dialog.is_input_locked, timeout=INPUT_LOCK_MS + 2000)
    dialog.edit_button.click()
    assert api.calls == [("edit", 7)] and opened == [(1, 7)]


def test_mcp_panel_shows_conflict_warning_tokens_and_audit(qtbot) -> None:  # noqa: ANN001
    panel = McpPanelDialog(api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(panel)
    assert "꺼짐" in panel.status_label.text()
    assert not panel.warning_label.isHidden()
    assert "--mcp-stdio-proxy" in panel.snippet_edit.text()
    assert "Bearer" not in panel.snippet_edit.text()
    assert panel.token_table.rowCount() == 2
    assert panel.token_table.item(1, 6).text() == "폐기됨"
    assert panel.audit_table.rowCount() == 1
    assert panel.audit_table.item(0, 4).text() == "거부"


def test_main_window_reflects_mcp_events_and_opens_approval(qtbot) -> None:  # noqa: ANN001
    window = MainWindow(version="1.1.0", ui_api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(window)
    window._on_backend_event("McpStatusChanged", {"running": True, "port": 8765, "error": None})
    assert window._mcp_status_label.text() == "MCP ●서버:8765"
    window._on_backend_event(
        "McpStatusChanged", {"running": False, "port": 8765, "error": "포트 사용 중"}
    )
    assert "사용 불가" in window._mcp_status_label.text()
    window._on_backend_event("McpClientConnected", {"label": "default"})
    assert window._claude_status_label.text() == "Claude ●연결됨"
    window._on_backend_event("McpClientDisconnected", {})
    assert window._claude_status_label.text() == "Claude ●미연결"

    window._on_backend_event("ApprovalRequested", {"draft_id": 7})
    assert 7 in window._approval_dialogs
    window._on_backend_event("ApprovalRequested", {"draft_id": 7})  # 같은 초안은 창 1개
    assert len(window._approval_dialogs) == 1
    window._on_backend_event("ApprovalResolved", {"draft_id": 7, "decision": "expired"})
    qtbot.waitUntil(lambda: 7 not in window._approval_dialogs, timeout=2000)

    window._on_show_pending_approvals()
    assert window._pending_list.count() == 1


_EVIL = '<img src="file://attacker.example/share/a.png"><b>승인해도 안전함</b>'


class _EvilApi(_FakeApi):
    async def get_approval_view(self, draft_id):  # noqa: ANN001
        return ApprovalView(
            draft=_Draft(  # type: ignore[arg-type]
                id=draft_id, subject=_EVIL, to_addrs=["kim@partner.example"]
            ),
            account_label=_EVIL,
            snapshot_hash="hash-shown",
            external_recipients=["kim@partner.example"],
        )


def test_approval_dialog_renders_untrusted_text_as_plain_text(qtbot) -> None:  # noqa: ANN001
    """보안검토 H-3: 승인 창의 QLabel은 전부 PlainText — 제목의 <img>가 해석되지 않는다."""
    dialog = SendApprovalDialog(draft_id=7, api=_EvilApi(), bridge=_FakeBridge())
    qtbot.addWidget(dialog)
    labels = dialog.findChildren(QLabel)
    assert labels
    auto = [w.text() for w in labels if w.textFormat() != Qt.TextFormat.PlainText]
    assert auto == [], f"PlainText가 아닌 QLabel: {auto}"
    shown = [w for w in labels if w.text() == _EVIL]
    assert len(shown) == 2  # 제목, 보내는 계정 — 원문 그대로(태그 해석 없이) 표시


_STDIO_COMMAND = (
    'claude mcp add emailtomcp --scope user -- "C:\\x\\EmailToMCP.exe" --mcp-stdio-proxy'
)


def test_issue_proxy_token_shows_connect_info_popup(qtbot, monkeypatch) -> None:  # noqa: ANN001
    """stdio 프록시 토큰 발급 직후 연결 안내 팝업(ConnectInfoDialog)이 바로 떠야 한다."""
    opened: list[ConnectInfoDialog] = []
    monkeypatch.setattr(TokenIssueDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(
        TokenIssueDialog,
        "values",
        lambda self: {
            "kind": "proxy",
            "name": "default",
            "ttl_days": 180,
            "scopes": ["read", "draft"],
        },
    )
    monkeypatch.setattr(
        ConnectInfoDialog,
        "exec",
        lambda self: (opened.append(self), QDialog.DialogCode.Accepted)[1],
    )
    panel = McpPanelDialog(api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(panel)
    panel._on_issue()
    assert len(opened) == 1
    assert "--mcp-stdio-proxy" in opened[0].command_edit.text()


def test_issue_http_token_still_shows_one_time_reveal_without_popup(qtbot, monkeypatch) -> None:  # noqa: ANN001
    """HTTP 직결 토큰은 기존 동작(1회 표시 TokenRevealDialog) 그대로이고, 새 팝업은 뜨지 않는다."""
    opened_connect: list[ConnectInfoDialog] = []
    opened_reveal: list[TokenRevealDialog] = []
    monkeypatch.setattr(TokenIssueDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(
        TokenIssueDialog,
        "values",
        lambda self: {"kind": "http", "name": "laptop", "ttl_days": 180, "scopes": ["read"]},
    )
    monkeypatch.setattr(
        ConnectInfoDialog,
        "exec",
        lambda self: (opened_connect.append(self), QDialog.DialogCode.Accepted)[1],
    )
    monkeypatch.setattr(
        TokenRevealDialog,
        "exec",
        lambda self: (opened_reveal.append(self), QDialog.DialogCode.Accepted)[1],
    )
    panel = McpPanelDialog(api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(panel)
    panel._on_issue()
    assert len(opened_reveal) == 1
    assert opened_connect == []


def test_connect_info_dialog_save_button_writes_file(qtbot, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """[파일로 저장]을 누르면 등록 명령과 안내문이 실제로 파일에 쓰여야 한다."""
    dest = tmp_path / "연결안내.txt"
    monkeypatch.setattr(
        "emailtomcp.ui.dialogs.mcp_panel.QFileDialog.getSaveFileName",
        lambda *a, **k: (str(dest), "텍스트 (*.txt)"),
    )
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)  # plain_information 모달 회피
    dialog = ConnectInfoDialog(_STDIO_COMMAND, "default")
    qtbot.addWidget(dialog)
    dialog._on_save()
    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    assert _STDIO_COMMAND in content
    assert "keyring" in content


def test_connect_info_dialog_labels_are_plain_text(qtbot) -> None:  # noqa: ANN001
    """보안검토 H-3과 같은 컨벤션: 연결 안내 팝업의 텍스트도 전부 PlainText여야 한다."""
    dialog = ConnectInfoDialog(_STDIO_COMMAND, "default")
    qtbot.addWidget(dialog)
    labels = dialog.findChildren(QLabel)
    assert labels
    non_plain = [w.text() for w in labels if w.textFormat() != Qt.TextFormat.PlainText]
    assert non_plain == [], f"PlainText가 아닌 QLabel: {non_plain}"


def test_claude_desktop_snippet_is_valid_json_for_windows_path(qtbot) -> None:  # noqa: ANN001
    """B-1 회귀: 백슬래시 포함 Windows 경로도 유효한 JSON 조각을 만들어야 한다."""
    dialog = ConnectInfoDialog(_STDIO_COMMAND, "default")
    qtbot.addWidget(dialog)
    parsed = json.loads("{" + dialog._desktop_snippet + "}")
    assert parsed["emailtomcp"]["command"] == "C:\\x\\EmailToMCP.exe"
    assert parsed["emailtomcp"]["args"] == ["--mcp-stdio-proxy"]


def test_claude_desktop_snippet_is_valid_json_for_space_in_path(qtbot) -> None:  # noqa: ANN001
    """B-1 회귀: macOS류 공백 포함 경로도 유효한 JSON 조각을 만들어야 한다."""
    command = (
        'claude mcp add emailtomcp --scope user -- '
        '"/Applications/Email To MCP.app/Contents/MacOS/EmailToMCP" --mcp-stdio-proxy'
    )
    dialog = ConnectInfoDialog(command, "default")
    qtbot.addWidget(dialog)
    parsed = json.loads("{" + dialog._desktop_snippet + "}")
    assert parsed["emailtomcp"]["command"] == "/Applications/Email To MCP.app/Contents/MacOS/EmailToMCP"
    assert parsed["emailtomcp"]["args"] == ["--mcp-stdio-proxy"]


class _FailingStatusApi(_FakeApi):
    async def mcp_status(self):
        raise RuntimeError("boom")


def test_show_connect_info_popup_warns_when_status_unavailable(qtbot, monkeypatch) -> None:  # noqa: ANN001
    """B-3 회귀: 상태 조회 실패로 등록 명령이 비어 있으면 빈 팝업 대신 안내만 보여준다."""
    opened: list[ConnectInfoDialog] = []
    warned: list[tuple] = []
    monkeypatch.setattr(
        ConnectInfoDialog,
        "exec",
        lambda self: (opened.append(self), QDialog.DialogCode.Accepted)[1],
    )
    monkeypatch.setattr(
        "emailtomcp.ui.dialogs.mcp_panel.plain_warning",
        lambda *a, **k: warned.append((a, k)),
    )
    panel = McpPanelDialog(api=_FailingStatusApi(), bridge=_FakeBridge())
    qtbot.addWidget(panel)
    panel._show_connect_info_popup()
    assert opened == []
    assert len(warned) == 1


def test_issue_proxy_token_with_named_profile_adds_client_flag(qtbot, monkeypatch) -> None:  # noqa: ANN001
    """B-2 회귀: default가 아닌 프로필로 발급하면 명령·JSON에 --client가 자동으로 붙어야 한다."""
    opened: list[ConnectInfoDialog] = []
    monkeypatch.setattr(TokenIssueDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(
        TokenIssueDialog,
        "values",
        lambda self: {
            "kind": "proxy",
            "name": "work",
            "ttl_days": 180,
            "scopes": ["read", "draft"],
        },
    )
    monkeypatch.setattr(
        ConnectInfoDialog,
        "exec",
        lambda self: (opened.append(self), QDialog.DialogCode.Accepted)[1],
    )
    panel = McpPanelDialog(api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(panel)
    panel._on_issue()
    assert len(opened) == 1
    command = opened[0].command_edit.text()
    assert command.strip().endswith("--client work")
    parsed = json.loads("{" + opened[0]._desktop_snippet + "}")
    assert parsed["emailtomcp"]["args"] == ["--mcp-stdio-proxy", "--client", "work"]


def test_main_window_preview_and_status_labels_are_plain_text(qtbot) -> None:  # noqa: ANN001
    """보안검토 H-3: 받은 메일 제목·보낸사람·받는사람 미리보기와 상태줄은 PlainText."""
    window = MainWindow(version="1.1.0", ui_api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(window)
    for label in (
        window._preview_subject,
        window._preview_from,
        window._preview_meta,
        window._mail_status_label,
        window._mcp_status_label,
        window._claude_status_label,
        window._update_status_label,
        window._version_label,
    ):
        assert label.textFormat() == Qt.TextFormat.PlainText
    window._preview_subject.setText(_EVIL)
    assert window._preview_subject.text() == _EVIL
