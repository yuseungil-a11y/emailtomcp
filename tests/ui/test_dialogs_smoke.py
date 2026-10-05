"""P1 신설 화면 스모크 테스트 (DESIGN.md §11.1~§11.3, §11.7, §11.9).

실제 Backend/DB 대신 최소 동작만 하는 가짜 `UiApi`/`bridge`를 주입해, 빈 상태에서
다이얼로그들이 예외 없이 뜨고 기본 동작(프리셋 선택, 설정 로드, 작성창 생성 등)이
되는지만 확인한다. 서비스 로직 자체는 `tests/unit/`에서 이미 검증한다.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass, field

import pytest
from PySide6.QtCore import Qt

from emailtomcp.ui.dialogs.account_dialog import AccountEditDialog, AccountManagerDialog
from emailtomcp.ui.dialogs.compose_window import ComposeWindow
from emailtomcp.ui.dialogs.manual_viewer import ManualViewer
from emailtomcp.ui.dialogs.raw_source_viewer import RawSourceViewer
from emailtomcp.ui.dialogs.settings_dialog import SettingsDialog
from emailtomcp.ui.main_window import MainWindow


class _FakeSignal:
    """`QtBridge.event_received`(Qt Signal)를 흉내낸 더미 — 이번 테스트는 이벤트 발행을
    검증하지 않으므로 `connect()`만 받아주면 된다."""

    def connect(self, _slot) -> None:  # noqa: ANN001
        pass


class _FakeBridge:
    """`qt_bridge.QtBridge`를 흉내낸다 — 코루틴을 즉시 동기 실행한다(테스트 전용)."""

    event_received = _FakeSignal()

    def call_backend(self, coro, timeout=5.0):  # noqa: ANN001, ARG002
        return asyncio.run(coro)

    def call_backend_async(self, coro):  # noqa: ANN001
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            result = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
        else:
            future.set_result(result)
        return future


@dataclass
class _FakeAccount:
    id: int = 1
    display_name: str = "테스트계정"
    email_address: str = "me@example.com"
    aliases: list[str] = field(default_factory=list)
    sender_name: str | None = None
    provider_preset: str | None = "custom"
    auth_method: str = "password"
    incoming_protocol: str = "imap"
    in_host: str = "imap.example.com"
    in_port: int = 993
    in_security: str = "ssl"
    in_username: str = "me@example.com"
    out_host: str = "smtp.example.com"
    out_port: int = 465
    out_security: str = "ssl"
    out_username: str | None = None
    out_auth_same_as_in: bool = True
    pop3_leave_on_server: bool = True
    pop3_delete_after_days: int | None = None
    poll_interval_sec: int = 300
    signature: str | None = None
    trusted_authserv_id: str | None = None
    auto_reply_enabled: bool = False
    enabled: bool = True


@dataclass
class _FakeDraft:
    id: int = 1
    account_id: int = 1
    kind: str = "new"
    source_message_id: int | None = None
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    bcc_addrs: list[str] = field(default_factory=list)
    subject: str | None = ""
    body_text: str | None = ""
    body_html: str | None = None
    attachments: list[dict] = field(default_factory=list)
    origin: str = "user"
    status: str = "draft"
    approval_hash: str | None = None
    message_id_hdr: str | None = None
    send_attempts: int = 0
    next_send_at: str | None = None
    sent_at: str | None = None
    sent_message_id: int | None = None
    last_error: str | None = None


class _FakeApi:
    """`emailtomcp.app.UiApi`의 테스트용 가짜 구현(필요한 메서드만)."""

    async def list_accounts(self):
        return [_FakeAccount()]

    async def get_account(self, account_id):
        return _FakeAccount(id=account_id)

    async def create_account(self, fields, password):
        return 1

    async def update_account(self, account_id, fields, password):
        return None

    async def test_incoming_connection(self, fields, password):
        return True, "연결 성공"

    async def test_outgoing_connection(self, fields, password):
        return True, "연결 성공"

    async def load_tree(self):
        return []

    async def fetch_now(self, account_id):
        return 0

    async def poll_all(self):
        return {}

    async def list_messages(self, folder_id, **kwargs):
        return [], 0

    async def list_outbox_drafts(self, account_id=None):
        return []

    async def get_message_detail(self, message_id):
        return None

    async def set_read(self, message_id, is_read):
        return None

    async def set_flagged(self, message_id, is_flagged):
        return None

    async def get_setting(self, key, default=None):
        return default

    async def set_setting(self, key, value):
        return None

    async def new_draft(self, account_id, **kwargs):
        return 1

    async def reply_draft(self, account_id, source_message_id, reply_all):
        return 1

    async def forward_draft(self, account_id, source_message_id, mode):
        return 1

    async def get_draft(self, draft_id):
        return _FakeDraft(id=draft_id)

    async def update_compose(self, draft_id, **fields):
        return True

    async def discard_draft(self, draft_id):
        return True

    async def stage_local_attachment(self, file_path):
        return {"source": "local_file", "path": file_path, "filename": "x.txt", "size_bytes": 1}

    async def send_draft_now(self, draft_id, **fields):
        # 작성 창은 화면 내용(fields)을 함께 넘겨야 한다(보안검토 N-1/R-1).
        self.sent_calls = [*getattr(self, "sent_calls", []), (draft_id, fields)]
        return "sent"

    async def list_known_addresses(self, account_id):
        return ["known@example.com"]


@pytest.mark.ui
def test_main_window_still_works_without_backend(qtbot) -> None:
    """기존 P0 스모크(백엔드 없음)를 그대로 유지한다."""
    window = MainWindow(version="1.0.0")
    qtbot.addWidget(window)
    window.show()
    assert window.isVisible()
    assert "1.0.0" in window._version_label.text()


@pytest.mark.ui
def test_main_window_with_fake_backend_loads_empty_tree(qtbot) -> None:
    window = MainWindow(version="1.0.0", ui_api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(window)
    window.show()
    assert window.isVisible()


@pytest.mark.ui
def test_account_manager_dialog_lists_accounts(qtbot) -> None:
    dialog = AccountManagerDialog(api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(dialog)
    assert dialog._list.count() == 1


@pytest.mark.ui
def test_account_edit_dialog_new_account_and_preset_selection(qtbot) -> None:
    dialog = AccountEditDialog(account_id=None, api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(dialog)
    dialog._preset_table.selectRow(0)  # "gmail" — 비활성 아님
    assert dialog._in_host.text() == "imap.gmail.com"


@pytest.mark.ui
def test_account_edit_dialog_disabled_preset_shows_notice(qtbot, monkeypatch) -> None:
    dialog = AccountEditDialog(account_id=None, api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(dialog)
    called = {}
    monkeypatch.setattr(
        "emailtomcp.ui.dialogs.account_dialog.QMessageBox.information",
        lambda *a, **k: called.setdefault("shown", True),
    )
    m365_row = dialog._preset_keys.index("m365")
    dialog._preset_table.selectRow(m365_row)
    assert called.get("shown") is True


@pytest.mark.ui
def test_settings_dialog_loads_and_applies_theme(qtbot) -> None:
    applied = []
    dialog = SettingsDialog(api=_FakeApi(), bridge=_FakeBridge(), apply_theme=applied.append)
    qtbot.addWidget(dialog)
    dialog._theme_combo.setCurrentIndex(1)  # light
    assert applied[-1] == "light"


@pytest.mark.ui
def test_compose_window_open_new_creates_draft(qtbot) -> None:
    window = ComposeWindow.open_new(1, api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(window)
    assert window._draft_id == 1


@pytest.mark.ui
def test_compose_window_open_reply_loads_fields(qtbot) -> None:
    window = ComposeWindow.open_reply(1, 5, reply_all=False, api=_FakeApi(), bridge=_FakeBridge())
    qtbot.addWidget(window)
    assert window._draft_id == 1


class _GuardApi(_FakeApi):
    def __init__(self, findings):  # noqa: ANN001
        self._findings = findings

    async def get_draft_guard_findings(self, draft_id):  # noqa: ANN001
        return self._findings


@pytest.mark.ui
def test_compose_existing_autoreply_draft_shows_guard_banner(qtbot) -> None:
    """데카르트 31번 M-3(§7.5 마지막 줄): 자동회신 초안의 출력 가드 결과를 작성 창 배너로."""
    window = ComposeWindow.open_existing(
        1, 7, api=_GuardApi(["url", "money", "account_number"]), bridge=_FakeBridge()
    )
    qtbot.addWidget(window)
    banner = window.guard_banner
    assert not banner.isHidden()
    assert banner.textFormat() == Qt.TextFormat.PlainText
    for label in ("링크(URL)", "금액", "계좌번호"):
        assert label in banner.text()


@pytest.mark.ui
@pytest.mark.parametrize("findings", [None, []])
def test_compose_existing_draft_without_findings_has_no_banner(qtbot, findings) -> None:  # noqa: ANN001
    """자동회신 초안이 아니거나(None) 가드 finding이 없으면 배너가 없다."""
    window = ComposeWindow.open_existing(1, 7, api=_GuardApi(findings), bridge=_FakeBridge())
    qtbot.addWidget(window)
    assert window.guard_banner.isHidden()


class _SourceApi(_GuardApi):
    def __init__(self, findings, source):  # noqa: ANN001
        super().__init__(findings)
        self._source = source

    async def get_autoreply_draft_source(self, draft_id):  # noqa: ANN001
        return self._source


@pytest.mark.ui
@pytest.mark.parametrize(
    ("source", "label_part", "claude_in_label", "claude_in_banner"),
    [
        (None, "Claude가 만든 초안", True, True),  # MCP [수정 후 발송] — 기존 문구 그대로
        ("claude", "자동회신 초안(Claude 작성)", True, True),
        ("fixed_template", "자동회신 초안(고정 템플릿)", False, False),
        ("unknown", "자동회신 초안 —", False, False),
        ("weird", "자동회신 초안 —", False, False),  # 모르는 값은 unknown 취급
    ],
)
def test_compose_existing_label_depends_on_claude_execution(
    qtbot,
    source,
    label_part,
    claude_in_label,
    claude_in_banner,  # noqa: ANN001
) -> None:
    """데카르트 33번 I-4: 고정 템플릿 자동회신 초안에는 "Claude가 만든" 문구를 쓰지 않는다."""
    window = ComposeWindow.open_existing(
        1, 7, api=_SourceApi(["url"], source), bridge=_FakeBridge()
    )
    qtbot.addWidget(window)
    label = window.origin_label_widget.text()
    assert label_part in label
    assert ("Claude" in label) is claude_in_label
    assert ("Claude" in window.guard_banner.text()) is claude_in_banner


@pytest.mark.ui
def test_compose_new_mail_has_no_guard_banner(qtbot) -> None:
    window = ComposeWindow.open_new(1, api=_GuardApi(["url"]), bridge=_FakeBridge())
    qtbot.addWidget(window)
    assert window.guard_banner.isHidden()


@pytest.mark.ui
def test_manual_viewer_shows_index(qtbot) -> None:
    viewer = ManualViewer()
    qtbot.addWidget(viewer)
    viewer.show_section("00_인덱스")
    assert "EmailToMCP" in viewer._browser.toPlainText()


@pytest.mark.ui
def test_raw_source_viewer_shows_plain_text(qtbot) -> None:
    viewer = RawSourceViewer(b"Subject: hi\r\n\r\nbody\r\n")
    qtbot.addWidget(viewer)


# ---------------------------------------------------------------- 2차 재검증 회귀(N-1/R-1, N-2)


@pytest.mark.ui
def test_compose_send_passes_shown_fields_in_one_call(qtbot) -> None:
    """N-1/R-1: [보내기]는 별도 자동저장 없이 화면 내용을 send_draft_now 한 번에 넘긴다."""
    api = _FakeApi()
    saves: list[dict] = []

    async def _update_compose(draft_id, **fields):  # noqa: ANN001, ANN202
        saves.append(fields)
        return True

    api.update_compose = _update_compose  # type: ignore[method-assign]
    window = ComposeWindow.open_new(1, api=api, bridge=_FakeBridge())
    qtbot.addWidget(window)
    window._to_edit.setText("kim@partner.example")
    window._bcc_edit.setText("")
    window._subject_edit.setText("제목")
    window._body_edit.setPlainText("본문")
    window._on_send()
    assert saves == []  # 저장과 발송이 분리된 별도 writer 작업이 없다
    assert len(api.sent_calls) == 1
    draft_id, fields = api.sent_calls[0]
    assert draft_id == 1
    assert fields["to_addrs"] == ["kim@partner.example"]
    assert fields["bcc_addrs"] == []
    assert fields["subject"] == "제목" and fields["body_text"] == "본문"
    assert fields["attachments"] == []


@pytest.mark.ui
def test_make_plain_message_box_sets_format_before_text(qtbot, monkeypatch) -> None:
    """S-2: PlainText 형식을 먼저 고정한 뒤에 텍스트를 넣는다(생성자에 텍스트를 바로
    넘기면 잠깐 AutoText 상태를 거치므로, 프로젝트의 다른 직접 생성 지점들과 순서를
    통일). `box.setText(text)`가 명시적으로 호출되는 시점에 이미 PlainText인지 확인한다
    — 예전 코드(`QMessageBox(icon, title, text, ...)`로 텍스트를 생성자에 넘김)는
    `setText`를 Python에서 명시적으로 호출하지 않아 이 스파이에 잡히지 않는다."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    from emailtomcp.ui.message_box import make_plain_message_box

    formats_when_text_set: list[Qt.TextFormat] = []
    original_set_text = QMessageBox.setText

    def _spy_set_text(self, text):  # noqa: ANN001
        formats_when_text_set.append(self.textFormat())
        return original_set_text(self, text)

    monkeypatch.setattr(QMessageBox, "setText", _spy_set_text)
    box = make_plain_message_box(None, "제목", "본문", QMessageBox.Icon.Warning)
    qtbot.addWidget(box)
    assert formats_when_text_set == [Qt.TextFormat.PlainText]
    assert box.text() == "본문"


@pytest.mark.ui
def test_compose_invalid_address_error_box_is_plain_text(qtbot, monkeypatch) -> None:
    """N-2: 메일에서 온 따옴표 주소가 들어간 주소 오류창은 평문(PlainText)으로만 표시한다."""
    from PySide6 import QtGui
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    shown: list[QMessageBox] = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self) or 0)
    api = _FakeApi()
    window = ComposeWindow.open_new(1, api=api, bridge=_FakeBridge())
    qtbot.addWidget(window)
    evil = '"<img src=//evil/s/a.png>"@evil.com'
    window._to_edit.setText(evil)
    window._on_send()

    assert len(shown) == 1
    box = shown[0]
    assert evil in box.text()
    # 이 문구는 AutoText였다면 리치텍스트로 해석된다(Spinoza 실측과 동일 조건).
    assert QtGui.Qt.mightBeRichText(box.text())
    assert box.textFormat() == Qt.TextFormat.PlainText
    assert getattr(api, "sent_calls", []) == []  # 발송 요청은 나가지 않는다


@pytest.mark.ui
def test_compose_send_failure_is_reported_in_plain_text(qtbot, monkeypatch) -> None:
    """N-1 ③: 저장(=발송 트랜잭션)이 실패하면 발송을 계속하지 않고 사용자에게 알린다."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    shown: list[QMessageBox] = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append(self) or 0)
    api = _FakeApi()

    async def _fail(draft_id, **fields):  # noqa: ANN001, ANN202
        raise RuntimeError("<b>저장 실패</b>")

    api.send_draft_now = _fail  # type: ignore[method-assign]
    window = ComposeWindow.open_new(1, api=api, bridge=_FakeBridge())
    qtbot.addWidget(window)
    window._to_edit.setText("kim@partner.example")
    window._on_send()
    qtbot.waitUntil(lambda: len(shown) == 1, timeout=3000)
    assert "저장 실패" in shown[0].text()
    assert shown[0].textFormat() == Qt.TextFormat.PlainText
    assert window.result() != window.DialogCode.Accepted
    assert window._send_btn.isEnabled()
