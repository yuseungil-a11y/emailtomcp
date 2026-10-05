"""Claude/MCP 패널 — MCP 부분 (DESIGN.md §11.5, §6.4, §6.5, §8.2, L6).

탭 구성
- **서버**: 서버 상태와 포트(충돌 시 경고), stdio 프록시 등록 명령(기본 연결 방식) 복사,
  HTTP 직결(고급) 안내, 대화형 세션 경고(§8.2). claude CLI 상태는 P3에서 제공한다.
- **토큰**: 라벨/종류/스코프/마지막 사용/만료/상태 목록, [발급]/[폐기]. 기본 발급
  스코프는 읽기+초안이고, 발송·관리는 명시적으로 체크해야 한다.
  - 프록시 토큰은 평문을 화면에 보여 주지 않는다(keyring에만 저장).
  - HTTP 직결 토큰은 발급 때 한 번만, 기본 마스킹 상태로 보여 준다. 복사할 때는 클립보드
    기록 제외 형식을 쓰고 60초 뒤 클립보드를 비운다(L6).
- **감사 로그**: 최근 MCP 호출(거부 포함) 200건.
- **잡**(P3 Phase B, §11.5): 자동회신 잡 목록(대기/실행 중/완료/취소/실패), 큐 상태와
  [일시정지]/[재개], claude CLI 고정 상태와 [claude CLI 찾기·고정]. 전역 토글이 꺼져 있으면
  [일시정지]/[재개]를 비활성화하고 안내를 보여 준다. 제목·보낸사람 셀은 평문이다(§7.10 H-3).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QMimeData, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from emailtomcp.ui.dialogs.compose_window import ComposeWindow
from emailtomcp.ui.dialogs.manual_viewer import ManualViewer
from emailtomcp.ui.message_box import plain_information, plain_warning

logger = logging.getLogger(__name__)

CLIPBOARD_CLEAR_MS = 60_000

SCOPE_CHOICES: tuple[tuple[str, str, bool], ...] = (
    ("read", "읽기(계정·메일 조회, 즉시 수신)", True),
    ("draft", "초안 작성·수정·삭제", True),
    ("send", "발송 요청(앱에서 승인 필요)", False),
    ("manage", "메일 관리(읽음·플래그·휴지통 이동)", False),
)
_SCOPE_SHORT = {"read": "읽기", "draft": "초안", "send": "발송", "manage": "관리"}

SESSION_WARNING = (
    "주의: 대화형 세션에서 메일을 읽으면, 메일 내용이 그 세션의 다른 도구(파일·셸 등)에도 "
    "영향을 줄 수 있습니다. 메일 본문에 들어 있는 지시를 Claude가 따르지 않도록 표시하지만, "
    "모델의 판단은 완벽하지 않습니다. 발송은 항상 이 앱의 승인 창에서 직접 확인하세요."
)

HTTP_DIRECT_GUIDE = (
    "HTTP 직결(고급): stdio 프록시 대신 URL로 직접 연결합니다. 토큰을 명령줄에 넣지 말고, "
    "Claude 설정 파일(JSON)을 직접 편집해 Authorization 헤더 값을 "
    '"Bearer ${EMAILTOMCP_TOKEN}" 처럼 환경변수 참조로 넣으세요. 셸 명령으로 등록하면 '
    "토큰이 셸 기록과 설정 파일에 평문으로 남으므로 등록 명령은 제공하지 않습니다."
)


JOBS_OFF_NOTICE = "자동회신이 꺼져 있습니다. 설정 > 자동회신 탭에서 켤 수 있습니다."
AUTOSEND_PHASE_B_NOTICE = (
    "이번 버전에서는 제공하지 않습니다 — 자동회신 결과는 모두 초안으로 저장됩니다."
)
QUEUE_LABELS = {
    "running": "실행 중",
    "paused_user": "일시정지(사용자)",
    "paused_auth": "일시정지(claude 로그인 필요)",
    "stopped_emergency": "긴급정지",
}
JOB_STATUS_LABELS = {
    "queued": "대기",
    "running": "실행 중",
    "done": "완료",
    "cancelled": "취소",
    "failed": "실패",
}
JOB_OUTCOME_LABELS = {
    "drafted": "초안 생성",
    "skipped": "건너뜀(Claude 판단)",
    "discarded": "폐기",
    "sent": "발송",
    "summarized": "요약",
}
REASON_LABELS = {
    "autosend_not_yet_supported": "즉시발송 미지원 → 초안",
    "isolation_fallback3": "claude 사용자 설정(hooks·플러그인 등) 감지로 실행 안 함",
    "claude_cli_unavailable": "claude CLI 미고정·변경됨",
    "mcp_unavailable": "MCP 서버 꺼짐",
    "disabled": "자동회신 끔",
    "disabled_at_startup": "꺼진 상태로 기동",
    "rule_deleted": "규칙 삭제",
    "rule_changed": "규칙 변경",
    "rule_disabled": "규칙 중지",
    "rule_missing": "규칙 없음",
    "account_disabled": "계정 자동회신 꺼짐",
    "generation_mismatch": "토글 세대 변경",
    "security_tool": "허용 외 도구 사용(보안 폐기)",
    "security_submit_count": "제출 2회 이상(보안 폐기)",
    "preflight_settings_changed": "실행 중 claude 설정 변경 감지(보안 폐기)",
    "preflight_unexpected_files": "잡 폴더에 예상 밖 파일 생성(보안 폐기)",
    "preflight_ancestor_unclean": "잡 폴더 상위에 claude 설정 발견",
    "preflight_jobdir_acl": "잡 폴더 권한 설정 실패",
    "timeout": "시간 초과",
    "submit_count": "제출 횟수 이상",
    "claude_exit": "claude 비정상 종료",
    # G7 판정기의 G6 재확인(Spinoza 34번 L-5) — 러너를 지나쳐 왔다면 비정상이라 보안 폐기
    "g6_tools_mismatch": "사후 검증 재확인: 허용 외 도구(보안 폐기)",
    "g6_submit_count_mismatch": "사후 검증 재확인: 제출 횟수 이상(보안 폐기)",
    "g6_exit_code_mismatch": "사후 검증 재확인: 비정상 종료(보안 폐기)",
    "g6_submission_missing": "사후 검증 재확인: 제출 기록 불일치(보안 폐기)",
}


def _plain(text: str, parent: QWidget | None = None) -> QLabel:
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setWordWrap(True)
    return label


class CliPinDialog(QDialog):
    """claude CLI 후보 중 하나를 사용자가 확인해 고정한다(§9.3 4번). 실행하지 않는다."""

    def __init__(self, candidates: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("claude CLI 찾기·고정")
        self.resize(640, 320)
        self._candidates = candidates
        layout = QVBoxLayout(self)
        layout.addWidget(
            _plain(
                "자동회신에 쓸 claude 실행 파일을 고릅니다. 고른 경로와 파일 지문(sha256)을 "
                "저장하고, 실행할 때마다 지문을 다시 확인합니다(바뀌면 다시 고정해야 합니다). "
                "목록에 없으면 직접 경로를 입력하세요. 이 창은 claude를 실행하지 않습니다.",
                self,
            )
        )
        self.list_widget = QListWidget(self)
        for item in candidates:
            text = item["program"] + (f"  +  {item['script']}" if item.get("script") else "")
            self.list_widget.addItem(QListWidgetItem(f"[{item.get('source', '')}] {text}"))
        layout.addWidget(self.list_widget)
        form = QFormLayout()
        self.program_edit = QLineEdit(self)
        self.program_edit.setPlaceholderText("직접 입력: claude.exe 또는 node.exe 절대경로")
        self.script_edit = QLineEdit(self)
        self.script_edit.setPlaceholderText("node로 실행할 때만: cli.js 절대경로")
        form.addRow("실행 파일", self.program_edit)
        form.addRow("cli.js", self.script_edit)
        layout.addLayout(form)
        self.list_widget.currentRowChanged.connect(self._on_row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_row(self, row: int) -> None:
        if 0 <= row < len(self._candidates):
            self.program_edit.setText(self._candidates[row]["program"])
            self.script_edit.setText(self._candidates[row].get("script") or "")

    def choice(self) -> dict | None:
        program = self.program_edit.text().strip()
        if not program:
            return None
        return {"program": program, "script": self.script_edit.text().strip() or None}


def copy_secret_to_clipboard(text: str) -> None:
    """클립보드 기록·동기화 제외 형식으로 복사하고, 60초 뒤 그대로면 지운다(L6)."""
    clipboard = QGuiApplication.clipboard()
    mime = QMimeData()
    mime.setText(text)
    if sys.platform == "win32":
        # Windows 클립보드 기록/클라우드 동기화 제외 형식(값은 DWORD 0).
        zero = QByteArray(b"\x00\x00\x00\x00")
        mime.setData(
            'application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"',
            zero,
        )
        mime.setData('application/x-qt-windows-mime;value="CanIncludeInClipboardHistory"', zero)
        mime.setData('application/x-qt-windows-mime;value="CanUploadToCloudClipboard"', zero)
    clipboard.setMimeData(mime)

    def _clear() -> None:
        if clipboard.text() == text:
            clipboard.clear()

    QTimer.singleShot(CLIPBOARD_CLEAR_MS, _clear)


class TokenIssueDialog(QDialog):
    """토큰 발급 입력 창."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MCP 토큰 발급")
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.kind_combo = QComboBox(self)
        self.kind_combo.addItem("stdio 프록시(권장)", "proxy")
        self.kind_combo.addItem("HTTP 직결(고급)", "http")
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        form.addRow("연결 방식", self.kind_combo)

        self.name_edit = QLineEdit("default", self)
        self._name_label = QLabel("프록시 프로필", self)
        form.addRow(self._name_label, self.name_edit)

        self.ttl_spin = QSpinBox(self)
        self.ttl_spin.setRange(1, 3650)
        self.ttl_spin.setValue(180)
        self.ttl_spin.setSuffix(" 일")
        form.addRow("만료", self.ttl_spin)
        layout.addLayout(form)

        layout.addWidget(QLabel("권한(스코프)", self))
        self.scope_checks: dict[str, QCheckBox] = {}
        for scope, label, default in SCOPE_CHOICES:
            check = QCheckBox(label, self)
            check.setChecked(default)
            self.scope_checks[scope] = check
            layout.addWidget(check)
        note = QLabel(
            "기본은 읽기+초안입니다. '발송 요청'과 '메일 관리'는 꼭 필요할 때만 켜세요.", self
        )
        note.setWordWrap(True)
        note.setProperty("role", "previewMeta")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_kind_changed(self, _index: int) -> None:
        if self.kind_combo.currentData() == "proxy":
            self._name_label.setText("프록시 프로필")
            if not self.name_edit.text().strip():
                self.name_edit.setText("default")
        else:
            self._name_label.setText("라벨")
            if self.name_edit.text().strip() == "default":
                self.name_edit.setText("")

    def values(self) -> dict[str, Any]:
        return {
            "kind": self.kind_combo.currentData(),
            "name": self.name_edit.text().strip(),
            "ttl_days": self.ttl_spin.value(),
            "scopes": [s for s, c in self.scope_checks.items() if c.isChecked()],
        }


class TokenRevealDialog(QDialog):
    """HTTP 직결 토큰 1회 표시(기본 마스킹)."""

    def __init__(self, token: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._token = token
        self.setWindowTitle("HTTP 직결 토큰 — 지금 한 번만 표시됩니다")
        layout = QVBoxLayout(self)
        info = QLabel(
            "이 창을 닫으면 토큰을 다시 볼 수 없습니다. 환경변수 EMAILTOMCP_TOKEN 등에 "
            "보관하고, 설정 파일에는 ${EMAILTOMCP_TOKEN} 참조만 넣으세요.",
            self,
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        self.token_edit = QLineEdit(token, self)
        self.token_edit.setReadOnly(True)
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.token_edit)
        row = QHBoxLayout()
        self.show_button = QPushButton("표시", self)
        self.show_button.setCheckable(True)
        self.show_button.toggled.connect(self._on_toggle)
        copy_button = QPushButton("복사(60초 뒤 자동 삭제)", self)
        copy_button.clicked.connect(lambda: copy_secret_to_clipboard(self._token))
        close_button = QPushButton("닫기", self)
        close_button.setProperty("variant", "secondary")
        close_button.clicked.connect(self.accept)
        row.addWidget(self.show_button)
        row.addWidget(copy_button)
        row.addStretch(1)
        row.addWidget(close_button)
        layout.addLayout(row)

    def _on_toggle(self, checked: bool) -> None:
        self.token_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self.show_button.setText("숨기기" if checked else "표시")


CONNECT_INFO_AFTER_TEXT = (
    "Claude Code를 다시 시작하면 연결됩니다. 메인 창 상태줄의 'Claude ●연결됨'이 켜지면 "
    "성공입니다. 이 명령에는 토큰이 들어 있지 않습니다 — 토큰은 OS keyring에만 저장됩니다."
)


def _build_claude_desktop_snippet(stdio_command: str) -> str:
    """stdio 등록 명령에서 실행 파일 경로·인자를 뽑아 Claude Desktop용 JSON 조각을 만든다.

    경로·인자 값은 `json.dumps`로 이스케이프한다(B-1) — Windows 경로(`C:\\...`)가 그대로
    문자열에 이어붙으면 역슬래시 때문에 `json.loads`가 실패하는 깨진 JSON이 만들어진다.
    """
    match = re.search(r'--\s+"([^"]+)"\s*(.*)$', stdio_command)
    if not match:
        return ""
    exe = match.group(1)
    rest = match.group(2).strip()
    try:
        args = shlex.split(rest) if rest else []
    except ValueError:
        # 따옴표가 짝이 안 맞는 등 shlex가 거부하는 입력은 공백 기준으로 대체한다.
        args = rest.split() if rest else []
    return (
        '"emailtomcp": {\n'
        f'  "command": {json.dumps(exe)},\n'
        f'  "args": {json.dumps(args)}\n'
        "}"
    )


class ConnectInfoDialog(QDialog):
    """stdio 프록시 토큰 발급 직후(또는 서버 탭 [연결안내 다시보기])에 보여주는 연결 안내 팝업.

    등록 명령에는 토큰이 들어 있지 않으므로(§6.4) 그대로 보여주고 파일로 저장해도 안전하다 —
    HTTP 직결 토큰(TokenRevealDialog)과 달리 1회 표시·자동 삭제 제약이 없다.
    """

    def __init__(
        self, stdio_command: str, profile_name: str | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._stdio_command = stdio_command
        self.setWindowTitle("Claude 연결 안내 — stdio 프록시")
        self.resize(640, 440)
        layout = QVBoxLayout(self)

        if profile_name:
            intro = (
                f"프록시 토큰(프로필 '{profile_name}')을 발급했습니다. 아래 명령을 터미널에서 "
                "한 번 실행하면 Claude Code가 연결됩니다."
            )
        else:
            intro = (
                "아래 명령을 터미널에서 한 번 실행하면 Claude Code가 연결됩니다(토큰 탭에서 "
                "stdio 프록시 토큰을 먼저 발급해 두세요)."
            )
        layout.addWidget(_plain(intro, self))

        self.command_edit = QLineEdit(stdio_command, self)
        self.command_edit.setReadOnly(True)
        layout.addWidget(self.command_edit)

        row = QHBoxLayout()
        copy_button = QPushButton("복사", self)
        copy_button.clicked.connect(self._on_copy)
        save_button = QPushButton("파일로 저장", self)
        save_button.clicked.connect(self._on_save)
        row.addWidget(copy_button)
        row.addWidget(save_button)
        row.addStretch(1)
        layout.addLayout(row)

        self._desktop_snippet = _build_claude_desktop_snippet(stdio_command)
        if self._desktop_snippet:
            layout.addWidget(
                _plain("Claude Desktop을 쓴다면 설정 JSON에 이렇게 추가하세요:", self)
            )
            desktop_label = _plain(self._desktop_snippet, self)
            desktop_label.setProperty("role", "previewMeta")
            layout.addWidget(desktop_label)

        layout.addWidget(_plain(CONNECT_INFO_AFTER_TEXT, self))
        layout.addStretch(1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton("닫기", self)
        close_button.setProperty("variant", "secondary")
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

    def _on_copy(self) -> None:
        QGuiApplication.clipboard().setText(self._stdio_command)

    def _file_content(self) -> str:
        lines = [
            "EmailToMCP — Claude 연결 안내(stdio 프록시)",
            "",
            "1. 아래 명령을 터미널에서 한 번 실행하세요.",
            "",
            self._stdio_command,
            "",
            "이 명령에는 토큰이 들어 있지 않습니다(토큰은 OS keyring에만 저장됩니다).",
        ]
        if self._desktop_snippet:
            lines += [
                "",
                "Claude Desktop을 쓴다면 설정 JSON에 이렇게 추가하세요:",
                self._desktop_snippet,
            ]
        lines += ["", CONNECT_INFO_AFTER_TEXT]
        return "\n".join(lines)

    def _on_save(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "연결 안내 저장",
            "emailtomcp_claude_연결안내.txt",
            "텍스트 (*.txt);;마크다운 (*.md);;모든 파일 (*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(self._file_content(), encoding="utf-8")
        except OSError as exc:
            plain_warning(self, "연결 안내 저장", f"저장하지 못했습니다: {exc}")
            return
        plain_information(self, "연결 안내 저장", f"저장했습니다: {path}")


class McpPanelDialog(QDialog):
    def __init__(self, *, api: Any, bridge: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._api = api
        self._bridge = bridge
        self.setWindowTitle("Claude/MCP 패널")
        self.resize(820, 560)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        self.tabs.addTab(self._build_server_tab(), "서버")
        self.tabs.addTab(self._build_tokens_tab(), "토큰")
        self.tabs.addTab(self._build_audit_tab(), "감사 로그")
        self.jobs_tab_index = self.tabs.addTab(self._build_jobs_tab(), "잡")

        close_row = QHBoxLayout()
        help_button = QPushButton("도움말", self)
        help_button.setProperty("variant", "secondary")
        help_button.clicked.connect(self._on_help)
        close_row.addWidget(help_button)
        close_row.addStretch(1)
        close_button = QPushButton("닫기", self)
        close_button.setProperty("variant", "secondary")
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        event_signal = getattr(bridge, "event_received", None)
        if event_signal is not None:
            event_signal.connect(self._on_backend_event)

        self.refresh_all()

    # ------------------------------------------------------------------ 서버 탭

    def _build_server_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.status_label = QLabel("확인 중...", tab)
        self.status_label.setProperty("statusDot", "connecting")
        form.addRow("MCP 서버", self.status_label)
        self.client_label = QLabel("", tab)
        form.addRow("Claude 연결", self.client_label)
        layout.addLayout(form)

        self.warning_label = QLabel("", tab)
        self.warning_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warning_label.setWordWrap(True)
        self.warning_label.setProperty("badge", "external")
        self.warning_label.setVisible(False)
        layout.addWidget(self.warning_label)

        layout.addWidget(
            QLabel("stdio 프록시 등록(기본, Claude Code) — 터미널에서 한 번 실행:", tab)
        )
        snippet_row = QHBoxLayout()
        self.snippet_edit = QLineEdit(tab)
        self.snippet_edit.setReadOnly(True)
        snippet_row.addWidget(self.snippet_edit, stretch=1)
        copy_button = QPushButton("복사", tab)
        copy_button.clicked.connect(self._on_copy_snippet)
        snippet_row.addWidget(copy_button)
        reshow_button = QPushButton("연결안내 다시보기", tab)
        reshow_button.setProperty("variant", "secondary")
        reshow_button.clicked.connect(lambda: self._show_connect_info_popup())
        snippet_row.addWidget(reshow_button)
        layout.addLayout(snippet_row)
        hint = QLabel(
            "등록 전에 '토큰' 탭에서 stdio 프록시 토큰(프로필 default)을 먼저 발급하세요. "
            "토큰은 keyring에만 저장되며 이 명령에는 들어가지 않습니다. 앱 포트를 바꿔도 "
            "다시 등록할 필요가 없습니다.",
            tab,
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "previewMeta")
        layout.addWidget(hint)

        http_label = QLabel(HTTP_DIRECT_GUIDE, tab)
        http_label.setWordWrap(True)
        layout.addWidget(http_label)

        warning = QLabel(SESSION_WARNING, tab)
        warning.setWordWrap(True)
        warning.setProperty("role", "previewMeta")
        layout.addWidget(warning)

        cli_label = QLabel(
            "자동회신에 쓰는 claude CLI 경로 고정은 '잡' 탭에서 합니다. 버전·인증 방식 표시와 "
            "[테스트]는 다음 업데이트에서 제공됩니다.",
            tab,
        )
        cli_label.setWordWrap(True)
        cli_label.setProperty("role", "previewMeta")
        layout.addWidget(cli_label)
        layout.addStretch(1)
        return tab

    def _on_copy_snippet(self) -> None:
        QGuiApplication.clipboard().setText(self.snippet_edit.text())

    def _show_connect_info_popup(self, profile_name: str | None = None) -> None:
        self.refresh_status()
        command = self.snippet_edit.text()
        if not command:
            # refresh_status()가 상태 조회 실패로 조용히 돌아온 경우(B-3) — 등록 명령이 빈
            # 채로 팝업이 뜨는 대신 안내만 보여준다.
            plain_warning(
                self,
                "Claude 연결 안내",
                "서버 상태를 확인하지 못해 등록 명령을 표시할 수 없습니다. 잠시 후 "
                "'서버' 탭에서 다시 시도하세요.",
            )
            return
        if profile_name and profile_name != "default":
            # default가 아닌 프로필은 등록 명령(및 Claude Desktop용 JSON args)에
            # `--client <프로필>`을 자동으로 붙여야 그 프로필로 연결된다(B-2).
            # 프로필 이름은 영문·숫자·_·-만 허용되므로(validate_proxy_client) 추가
            # 이스케이프 없이 그대로 이어붙여도 안전하다.
            command = f"{command} --client {profile_name}"
        ConnectInfoDialog(command, profile_name, self).exec()

    def refresh_status(self) -> None:
        try:
            status = self._bridge.call_backend(self._api.mcp_status(), timeout=3.0)
        except Exception:  # noqa: BLE001
            logger.warning("MCP 상태 조회 실패", exc_info=True)
            return
        port = status.get("port")
        if status.get("running"):
            self.status_label.setText(f"●실행 중 — 127.0.0.1:{port}")
            self.status_label.setProperty("statusDot", "connected")
            self.warning_label.setVisible(False)
        else:
            self.status_label.setText(f"●꺼짐 (포트 {port})")
            self.status_label.setProperty("statusDot", "disconnected")
            error = status.get("error")
            if error:
                self.warning_label.setText(
                    f"MCP 서버를 시작하지 못했습니다: {error}\n"
                    "다른 프로그램이 같은 포트를 쓰고 있을 수 있습니다. 포트는 자동으로 바뀌지 "
                    "않습니다 — 도구 > 설정 > MCP 탭에서 포트를 바꾼 뒤 앱을 다시 시작하세요."
                )
                self.warning_label.setVisible(True)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.client_label.setText("●연결됨" if status.get("client_connected") else "●미연결")
        self.snippet_edit.setText(str(status.get("stdio_command", "")))

    # ------------------------------------------------------------------ 토큰 탭

    def _build_tokens_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        self.token_table = QTableWidget(0, 7, tab)
        self.token_table.setHorizontalHeaderLabels(
            ["ID", "라벨/프로필", "종류", "스코프", "마지막 사용", "만료", "상태"]
        )
        self.token_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.token_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.token_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.token_table)
        row = QHBoxLayout()
        issue_button = QPushButton("발급", tab)
        issue_button.setProperty("variant", "primary")
        issue_button.clicked.connect(self._on_issue)
        revoke_button = QPushButton("폐기", tab)
        revoke_button.setProperty("variant", "destructive")
        revoke_button.clicked.connect(self._on_revoke)
        refresh_button = QPushButton("새로고침", tab)
        refresh_button.setProperty("variant", "secondary")
        refresh_button.clicked.connect(self.refresh_tokens)
        row.addWidget(issue_button)
        row.addWidget(revoke_button)
        row.addStretch(1)
        row.addWidget(refresh_button)
        layout.addLayout(row)
        return tab

    def refresh_tokens(self) -> None:
        try:
            tokens = self._bridge.call_backend(self._api.list_mcp_tokens(), timeout=5.0)
        except Exception:  # noqa: BLE001
            logger.warning("MCP 토큰 목록 조회 실패", exc_info=True)
            return
        self.token_table.setRowCount(len(tokens))
        for r, token in enumerate(tokens):
            state = "폐기됨" if token.revoked_at else "유효"
            values = [
                str(token.id),
                token.label,
                "프록시" if token.kind == "proxy" else "HTTP",
                ", ".join(_SCOPE_SHORT.get(s, s) for s in token.scopes),
                (token.last_used_at or "-")[:19],
                (token.expires_at or "-")[:10],
                state,
            ]
            for c, value in enumerate(values):
                self.token_table.setItem(r, c, QTableWidgetItem(value))

    def _selected_token_id(self) -> int | None:
        row = self.token_table.currentRow()
        if row < 0:
            return None
        item = self.token_table.item(row, 0)
        return int(item.text()) if item else None

    def _on_issue(self) -> None:
        dialog = TokenIssueDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values["scopes"]:
            QMessageBox.warning(self, "토큰 발급", "권한을 하나 이상 선택하세요.")
            return
        if "send" in values["scopes"] or "manage" in values["scopes"]:
            answer = QMessageBox.question(
                self,
                "토큰 발급",
                "발송 요청/메일 관리 권한을 포함합니다. 계속하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            issued = self._bridge.call_backend(
                self._api.issue_mcp_token(
                    kind=values["kind"],
                    scopes=values["scopes"],
                    name=values["name"],
                    ttl_days=values["ttl_days"],
                ),
                timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "토큰 발급", f"발급하지 못했습니다: {exc}")
            return
        if issued.plaintext:
            TokenRevealDialog(issued.plaintext, self).exec()
        else:
            self._show_connect_info_popup(values["name"] or "default")
        self.refresh_tokens()

    def _on_revoke(self) -> None:
        token_id = self._selected_token_id()
        if token_id is None:
            QMessageBox.information(self, "토큰 폐기", "폐기할 토큰을 선택하세요.")
            return
        answer = QMessageBox.question(
            self,
            "토큰 폐기",
            f"토큰 #{token_id}를 폐기합니다. 이 토큰을 쓰는 Claude 연결은 바로 끊깁니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._bridge.call_backend(self._api.revoke_mcp_token(token_id), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "토큰 폐기", f"폐기하지 못했습니다: {exc}")
            return
        self.refresh_tokens()

    # ------------------------------------------------------------------ 감사 탭

    def _build_audit_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        self.audit_table = QTableWidget(0, 7, tab)
        self.audit_table.setHorizontalHeaderLabels(
            ["시각", "주체", "토큰", "도구", "결과", "HTTP", "상세"]
        )
        self.audit_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.audit_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.audit_table)
        row = QHBoxLayout()
        row.addWidget(QLabel("본문·제목은 길이와 해시만 기록됩니다. 최근 200건.", tab))
        row.addStretch(1)
        refresh_button = QPushButton("새로고침", tab)
        refresh_button.setProperty("variant", "secondary")
        refresh_button.clicked.connect(self.refresh_audit)
        row.addWidget(refresh_button)
        layout.addLayout(row)
        return tab

    def refresh_audit(self) -> None:
        try:
            rows = self._bridge.call_backend(self._api.list_mcp_audit(200), timeout=5.0)
        except Exception:  # noqa: BLE001
            logger.warning("MCP 감사 로그 조회 실패", exc_info=True)
            return
        result_labels = {"ok": "성공", "denied": "거부", "error": "오류"}
        self.audit_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            token = f"#{row.token_id} ({row.token_hash8})" if row.token_id else "-"
            values = [
                row.ts[:19],
                "대화형" if row.principal == "interactive" else "잡",
                token,
                row.tool or "-",
                result_labels.get(row.result, row.result),
                str(row.http_status or ""),
                row.detail or "",
            ]
            for c, value in enumerate(values):
                self.audit_table.setItem(r, c, QTableWidgetItem(value))

    # ------------------------------------------------------------------ 잡 탭(§11.5)

    def _build_jobs_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        self.jobs_off_label = _plain(JOBS_OFF_NOTICE, tab)
        self.jobs_off_label.setProperty("badge", "external")
        layout.addWidget(self.jobs_off_label)
        form = QFormLayout()
        self.queue_label = _plain("", tab)
        form.addRow("큐 상태", self.queue_label)
        self.autosend_label = _plain(AUTOSEND_PHASE_B_NOTICE, tab)
        form.addRow("자동발송", self.autosend_label)
        self.cli_status_label = _plain("", tab)
        form.addRow("claude CLI", self.cli_status_label)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.pause_button = QPushButton("일시정지", tab)
        self.pause_button.setProperty("variant", "secondary")
        self.pause_button.clicked.connect(lambda: self._on_set_paused(True))
        self.resume_button = QPushButton("재개", tab)
        self.resume_button.setProperty("variant", "secondary")
        self.resume_button.clicked.connect(lambda: self._on_set_paused(False))
        self.pin_button = QPushButton("claude CLI 찾기·고정", tab)
        self.pin_button.setProperty("variant", "secondary")
        self.pin_button.clicked.connect(self._on_pin_cli)
        refresh_button = QPushButton("새로고침", tab)
        refresh_button.setProperty("variant", "secondary")
        refresh_button.clicked.connect(self.refresh_jobs)
        # 결과가 "초안 생성"인 잡의 초안을 작성 창으로 연다(출력 가드 경고 배너 포함, §7.5).
        self.open_draft_button = QPushButton("초안 열기", tab)
        self.open_draft_button.setProperty("variant", "secondary")
        self.open_draft_button.setEnabled(False)
        self.open_draft_button.clicked.connect(self._on_open_draft)
        row.addWidget(self.pause_button)
        row.addWidget(self.resume_button)
        row.addWidget(self.pin_button)
        row.addWidget(self.open_draft_button)
        row.addStretch(1)
        row.addWidget(refresh_button)
        layout.addLayout(row)
        self.jobs_table = QTableWidget(0, 8, tab)
        self.jobs_table.setHorizontalHeaderLabels(
            ["ID", "만든 시각", "보낸사람", "제목", "규칙", "상태", "결과", "사유"]
        )
        self.jobs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.jobs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs_table.itemSelectionChanged.connect(self._update_open_draft_button)
        self.jobs_table.cellDoubleClicked.connect(lambda _r, _c: self._on_open_draft())
        layout.addWidget(self.jobs_table)
        note = _plain(
            "자동회신 결과는 이번 버전에서 모두 '초안'으로 저장됩니다. 결과가 '초안 생성'인 줄을 "
            "고르고 [초안 열기](또는 더블클릭)로 작성 창에서 검토한 뒤 직접 보내세요. "
            "제목·보낸사람은 받은 메일에서 온 값이라 평문으로만 표시합니다.",
            tab,
        )
        note.setProperty("role", "previewMeta")
        layout.addWidget(note)
        return tab

    def refresh_jobs(self) -> None:
        status_getter = getattr(self._api, "get_autoreply_queue_status", None)
        jobs_getter = getattr(self._api, "list_autoreply_jobs", None)
        if status_getter is None or jobs_getter is None:
            self.jobs_off_label.setVisible(True)
            self.pause_button.setEnabled(False)
            self.resume_button.setEnabled(False)
            self.pin_button.setEnabled(False)
            return
        try:
            status = self._bridge.call_backend(status_getter(), timeout=5.0)
            jobs = self._bridge.call_backend(jobs_getter(100), timeout=5.0)
        except Exception:  # noqa: BLE001
            logger.warning("자동회신 잡 조회 실패", exc_info=True)
            return
        enabled = bool(status.get("enabled"))
        queue_state = status.get("queue_state")
        self.jobs_off_label.setVisible(not enabled)
        self.pause_button.setEnabled(enabled and queue_state != "paused_user")
        self.resume_button.setEnabled(enabled and queue_state == "paused_user")
        tooltip = "" if enabled else JOBS_OFF_NOTICE
        self.pause_button.setToolTip(tooltip)
        self.resume_button.setToolTip(tooltip)
        counts = status.get("counts") or {}
        self.queue_label.setText(
            f"{QUEUE_LABELS.get(queue_state, '미설정(켜면 실행 중으로 시작)')} — "
            f"대기 {counts.get('queued', 0)} · 실행 중 {counts.get('running', 0)} · "
            f"완료 {counts.get('done', 0)} · 취소 {counts.get('cancelled', 0)} · "
            f"실패 {counts.get('failed', 0)}"
        )
        cli = status.get("cli") or {}
        if cli.get("pinned") and not cli.get("problem"):
            program = cli.get("program") or ""
            script = cli.get("script")
            self.cli_status_label.setText(f"고정됨 — {program}" + (f" {script}" if script else ""))
        elif cli.get("pinned"):
            self.cli_status_label.setText(f"확인 필요 — {cli.get('problem')}")
        else:
            self.cli_status_label.setText(
                "미고정 — 'Claude가 작성' 규칙은 실행되지 않습니다(고정 템플릿 규칙만 동작)"
            )
        self.jobs_table.setRowCount(len(jobs))
        for r, job in enumerate(jobs):
            reason = job.error or job.downgrade_reason or ""
            values = [
                str(job.id),
                (job.created_at or "")[:19],
                job.from_addr or "",
                job.subject or "",
                job.rule_name or ("(미매칭 기본)" if job.kind == "auto_reply" else ""),
                JOB_STATUS_LABELS.get(job.status, job.status),
                JOB_OUTCOME_LABELS.get(job.outcome or "", job.outcome or ""),
                REASON_LABELS.get(reason, reason),
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, job.result_draft_id)
                self.jobs_table.setItem(r, c, item)
        self._update_open_draft_button()

    def _selected_result_draft_id(self) -> int | None:
        row = self.jobs_table.currentRow()
        if row < 0 or not self.jobs_table.selectionModel().hasSelection():
            return None
        item = self.jobs_table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, int) else None

    def _update_open_draft_button(self) -> None:
        self.open_draft_button.setEnabled(self._selected_result_draft_id() is not None)

    def _on_open_draft(self) -> None:
        draft_id = self._selected_result_draft_id()
        if draft_id is None:
            return
        try:
            draft = self._bridge.call_backend(self._api.get_draft(draft_id), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "초안 열기", f"초안을 불러오지 못했습니다: {exc}")
            return
        if draft is None or draft.status != "draft":
            plain_information(self, "초안 열기", "이 초안은 이미 보냈거나 삭제되어 열 수 없습니다.")
            return
        window = ComposeWindow.open_existing(
            draft.account_id, draft_id, api=self._api, bridge=self._bridge, parent=self
        )
        window.show()

    def _on_set_paused(self, paused: bool) -> None:
        try:
            self._bridge.call_backend(self._api.set_autoreply_queue_paused(paused), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "자동회신 큐", f"바꾸지 못했습니다: {exc}")
        self.refresh_jobs()

    def _on_pin_cli(self) -> None:
        try:
            candidates = self._bridge.call_backend(self._api.discover_claude_cli(), timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "claude CLI", f"찾지 못했습니다: {exc}")
            return
        dialog = CliPinDialog(candidates, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        choice = dialog.choice()
        if choice is None:
            return
        try:
            self._bridge.call_backend(
                self._api.pin_claude_cli(choice["program"], choice.get("script")), timeout=30.0
            )
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "claude CLI", f"고정하지 못했습니다: {exc}")
            return
        self.refresh_jobs()

    # ------------------------------------------------------------------ 공통

    def refresh_all(self) -> None:
        self.refresh_status()
        self.refresh_tokens()
        self.refresh_audit()
        self.refresh_jobs()

    def _on_help(self) -> None:
        viewer = ManualViewer(parent=self)
        viewer.show_section("05_Claude_MCP패널")
        viewer.exec()

    def _on_backend_event(self, name: str, _payload: object) -> None:
        if name in ("McpStatusChanged", "McpClientConnected", "McpClientDisconnected"):
            self.refresh_status()
        if name in ("JobFinished", "AutoReplyEnabledChanged"):
            self.refresh_jobs()
