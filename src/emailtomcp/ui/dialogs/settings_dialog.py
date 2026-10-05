"""설정 다이얼로그 (DESIGN.md §11.7).

수신 주기(신규 계정 기본값)·미리보기 위치·테마·업데이트(자동 확인 on/off, [지금 확인],
현재 버전·마지막 확인 결과)는 실제로 동작한다. 프록시는 입력란만 두고 저장만 한다(U2).

자동회신 탭(§11.7, §7.0): 맨 위에 전역 토글 [자동회신 사용]과 상태 라벨(평문)을 둔다.
- 토글은 범용 `set_setting`이 아니라 전용 API(`enable_autoreply`/`disable_autoreply`)로만
  바꾼다(보호 키).
- off → on 저장: 확인창(기본 버튼 [취소], [켜기]는 1.5초 입력잠금 — 발송 승인 창과 같은 방식).
  탭을 열 때 읽은 세대(`generation`)를 CAS 값으로 보내고, 실패하면 평문 경고 후 다시 읽는다.
- on → off 저장: 확인창 없이 바로 적용하고, 결과를 평문 정보창으로 보여 준다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from emailtomcp.ui import async_utils
from emailtomcp.ui.dialogs.send_approval_dialog import INPUT_LOCK_MS
from emailtomcp.ui.message_box import plain_information, plain_warning
from emailtomcp.ui.widgets.update_banner import describe_update_status

_THEME_OPTIONS = (("system", "시스템 설정 따름"), ("light", "라이트"), ("dark", "다크"))
_PREVIEW_OPTIONS = (("bottom", "그리드 아래"), ("right", "그리드 옆"))
# MCP 발송 모드(§6.2). 기본은 confirm(확정).
_MCP_SEND_MODE_OPTIONS = (
    ("confirm", "확인(발송마다 승인 창, 기본)"),
    ("allowlist", "허용 목록(목록 안 수신자만 승인 없이)"),
    ("disabled", "사용 안 함(초안만 작성)"),
)
_DEFAULT_MCP_PORT = 8765
# 탭 식별자(메인 창 상태줄 클릭 → 설정 > 자동회신 탭 바로 열기, §11.1).
TAB_AUTOREPLY = "autoreply"


def _plain(text: str, parent: QWidget) -> QLabel:
    """평문 고정 QLabel(§11.7 "상태 라벨은 PlainText")."""
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setText(text)
    return label


def describe_autoreply_state(view: dict | None) -> str:
    """상태 라벨 문구(§11.7 목업: "상태: ○ 꺼짐 — 규칙 3개 보존됨 (마지막 변경 10-05 14:02)")."""
    view = view or {}
    on = bool(view.get("enabled"))
    parts = ["상태: ● 켜짐" if on else "상태: ○ 꺼짐"]
    rules_total = (view.get("preflight") or {}).get("rules_total")
    if isinstance(rules_total, int):
        parts.append(f" — 규칙 {rules_total}개 {'적용 중' if on else '보존됨'}")
    changed = view.get("changed_at")
    if isinstance(changed, str) and changed:
        try:
            shown = datetime.fromisoformat(changed).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            shown = changed[:16].replace("T", " ")
        parts.append(f" (마지막 변경 {shown})")
    return "".join(parts)


def describe_preflight(preflight: dict | None) -> str:
    """켜기 확인창의 사전점검 목록(§7.0 켜기 1단계). 켜기를 막지는 않는다."""
    p = preflight or {}

    def _n(key: str) -> str:
        value = p.get(key)
        return str(value) if isinstance(value, int) else "해당없음"

    cli = p.get("claude_cli_pinned")
    cli_text = "해당없음(이후 단계에서 확인)" if cli is None else ("고정됨" if cli else "미고정")
    unmatched = "초안 만들기" if p.get("unmatched_action") == "draft" else "무시(기본)"
    return "\n".join(
        [
            f"- 자동회신을 쓰는 계정: {_n('autoreply_accounts')}개",
            f"- 활성 규칙: {_n('rules_enabled')}개 (그중 즉시발송 {_n('rules_auto_send')}개)",
            "- 인증 설정(trusted_authserv_id 등)이 없어 즉시발송이 초안으로 바뀌는 계정: "
            f"{_n('accounts_without_trust')}개",
            f"- claude CLI 고정: {cli_text}",
            f"- 규칙에 맞지 않는 메일 처리: {unmatched}",
        ]
    )


class AutoReplyEnableConfirmDialog(QDialog):
    """off → on 저장 시 확인창(§11.7). 기본 버튼 [취소], 창이 뜬 뒤 1.5초 동안 입력 잠금.

    발송 승인 창(§11.6, M5)과 같은 입력잠금 방식이다: 잠금 중에는 버튼 비활성 + 키 입력 무시 —
    다른 창에서 치던 Enter로 켜지는 사고를 막는다. 모든 문구는 평문(PlainText)이다.
    """

    def __init__(self, preflight: dict | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("자동회신 켜기")
        self._input_locked = True
        self._lock_started = False

        layout = QVBoxLayout(self)
        layout.addWidget(
            _plain(
                "자동회신을 켜면 지금 이후에 받은 메일에만 규칙이 적용되고, 규칙에 따라 "
                "메일이 자동으로 발송될 수 있습니다. 켜기 전에 아래 내용을 확인하세요.",
                self,
            )
        )
        self.preflight_label = _plain(describe_preflight(preflight), self)
        layout.addWidget(self.preflight_label)
        layout.addWidget(
            _plain("꺼져 있는 동안 받은 메일에는 켠 뒤에도 소급 적용하지 않습니다.", self)
        )

        buttons = QHBoxLayout()
        self.cancel_button = QPushButton("취소", self)
        self.cancel_button.setProperty("variant", "secondary")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.enable_button = QPushButton("켜기", self)
        self.enable_button.setProperty("variant", "primary")
        self.enable_button.setAutoDefault(False)
        self.enable_button.clicked.connect(self._on_enable_clicked)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.enable_button)
        layout.addLayout(buttons)

        self.enable_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self._unlock_timer = QTimer(self)
        self._unlock_timer.setSingleShot(True)
        self._unlock_timer.setInterval(INPUT_LOCK_MS)
        self._unlock_timer.timeout.connect(self._unlock_inputs)

    @property
    def is_input_locked(self) -> bool:
        return self._input_locked

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — Qt 오버라이드
        super().showEvent(event)
        if not self._lock_started:
            self._lock_started = True
            self._unlock_timer.start()

    def _unlock_inputs(self) -> None:
        self._input_locked = False
        self.enable_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 — Qt 오버라이드
        if self._input_locked:
            event.ignore()
            return
        super().keyPressEvent(event)

    def _on_enable_clicked(self) -> None:
        if self._input_locked:
            return
        self.accept()


class SettingsDialog(QDialog):
    def __init__(
        self,
        *,
        api: Any,
        bridge: Any,
        apply_theme: Any,
        initial_tab: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.resize(520, 460)
        self._api = api
        self._bridge = bridge
        self._apply_theme = apply_theme
        self._update_auto_loaded: bool | None = None
        # 자동회신 토글: 탭을 열 때 읽은 스냅샷(켜기 CAS의 기준 세대). None이면 토글을 잠근다.
        self._autoreply_view: dict | None = None

        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        self._tabs = tabs
        layout.addWidget(tabs)

        general_tab = QWidget(self)
        general_form = QFormLayout(general_tab)
        self._poll_interval = QSpinBox(general_tab)
        self._poll_interval.setRange(1, 1440)
        self._poll_interval.setSuffix(" 분")
        general_form.addRow("수신 주기(신규 계정 기본값)", self._poll_interval)

        self._preview_position = QComboBox(general_tab)
        for value, label in _PREVIEW_OPTIONS:
            self._preview_position.addItem(label, value)
        general_form.addRow("미리보기 위치", self._preview_position)

        self._theme_combo = QComboBox(general_tab)
        for value, label in _THEME_OPTIONS:
            self._theme_combo.addItem(label, value)
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        general_form.addRow("테마", self._theme_combo)
        tabs.addTab(general_tab, "일반")

        update_tab = QWidget(self)
        update_form = QFormLayout(update_tab)
        self._update_current_version = QLabel("-", update_tab)
        update_form.addRow("현재 버전", self._update_current_version)
        update_form.addRow("채널", QLabel("stable (안정판만 제공)", update_tab))
        self._update_auto_check = QCheckBox(
            "자동으로 업데이트 확인(앱 시작 30초 뒤, 이후 24시간마다)", update_tab
        )
        update_form.addRow(self._update_auto_check)
        self._update_last_checked = QLabel("-", update_tab)
        update_form.addRow("마지막 확인", self._update_last_checked)
        self._update_result = QLabel("아직 확인하지 않았습니다.", update_tab)
        self._update_result.setWordWrap(True)
        self._update_result.setTextFormat(Qt.TextFormat.PlainText)
        update_form.addRow("확인 결과", self._update_result)
        self._update_check_now = QPushButton("지금 확인", update_tab)
        self._update_check_now.clicked.connect(self._on_check_update_now)
        update_form.addRow(self._update_check_now)
        update_note = QLabel(
            "새 버전이 있으면 메인 창 위쪽 배너의 [다운로드 페이지 열기]로 받아 설치합니다. "
            "앱 안에서 자동으로 내려받아 설치하는 기능은 이후 버전(U2)에서 제공됩니다.",
            update_tab,
        )
        update_note.setWordWrap(True)
        update_form.addRow(update_note)
        tabs.addTab(update_tab, "업데이트")

        # MCP 탭(§11.7 "MCP 발송 모드(기본 confirm), 포트", §6.2 allowlist)
        mcp_tab = QWidget(self)
        mcp_form = QFormLayout(mcp_tab)
        self._mcp_send_mode = QComboBox(mcp_tab)
        for value, label in _MCP_SEND_MODE_OPTIONS:
            self._mcp_send_mode.addItem(label, value)
        mcp_form.addRow("발송 모드", self._mcp_send_mode)
        self._mcp_allowlist = QPlainTextEdit(mcp_tab)
        self._mcp_allowlist.setPlaceholderText("한 줄에 하나씩: user@example.com 또는 example.com")
        self._mcp_allowlist.setMaximumHeight(110)
        mcp_form.addRow("허용 목록", self._mcp_allowlist)
        allowlist_note = QLabel(
            "허용 목록 모드에서는 받는사람·참조·숨은참조 모두가 목록에 있을 때만 승인 없이 "
            "보냅니다. 도메인은 정확히 일치해야 하며(하위 도메인 제외), 첨부가 포함된 전달은 "
            "항상 승인 창이 뜹니다. Claude를 통한 발송은 시간당 20건·하루 100건까지입니다.",
            mcp_tab,
        )
        allowlist_note.setWordWrap(True)
        mcp_form.addRow(allowlist_note)
        self._mcp_port = QSpinBox(mcp_tab)
        self._mcp_port.setRange(1024, 65535)
        self._mcp_port.setValue(_DEFAULT_MCP_PORT)
        mcp_form.addRow("포트(127.0.0.1)", self._mcp_port)
        port_note = QLabel(
            "포트는 앱을 다시 시작하면 적용됩니다. 사용 중인 포트면 자동으로 바꾸지 않고 MCP를 "
            "끈 채 경고합니다. stdio 프록시로 연결했다면 Claude 쪽은 다시 등록할 필요가 없습니다.",
            mcp_tab,
        )
        port_note.setWordWrap(True)
        mcp_form.addRow(port_note)
        tabs.addTab(mcp_tab, "MCP")

        # 자동회신 탭(§11.7 목업). 전역 토글은 탭 맨 위, 위젯은 업데이트 탭과 같은 QCheckBox.
        autoreply_tab = QWidget(self)
        autoreply_form = QFormLayout(autoreply_tab)
        self._autoreply_enabled = QCheckBox("자동회신 사용", autoreply_tab)
        autoreply_form.addRow(self._autoreply_enabled)
        self._autoreply_state_label = _plain("상태: 확인 중", autoreply_tab)
        autoreply_form.addRow(self._autoreply_state_label)
        autoreply_form.addRow(
            _plain(
                '켜면 "켠 이후에 받은 메일"에만 규칙이 적용됩니다.\n'
                "꺼도 규칙과 계정별 설정은 지워지지 않습니다.",
                autoreply_tab,
            )
        )
        separator = QFrame(autoreply_tab)
        separator.setFrameShape(QFrame.Shape.HLine)
        autoreply_form.addRow(separator)
        self._autoreply_timeout = QSpinBox(autoreply_tab)
        self._autoreply_timeout.setRange(30, 600)
        self._autoreply_timeout.setSuffix(" 초")
        autoreply_form.addRow("자동회신 타임아웃", self._autoreply_timeout)
        autoreply_form.addRow(
            _plain(
                "규칙 설정 화면은 이후 단계에서 제공됩니다. 지금은 자동회신을 켜도 적용할 규칙이 "
                "없어 실제로 보내는 메일은 없습니다.",
                autoreply_tab,
            )
        )
        self._autoreply_tab_index = tabs.addTab(autoreply_tab, "자동회신")

        proxy_tab = QWidget(self)
        proxy_form = QFormLayout(proxy_tab)
        self._proxy_url = QLineEdit(proxy_tab)
        self._proxy_url.setPlaceholderText("http://proxy.example.com:8080")
        proxy_form.addRow("HTTPS_PROXY", self._proxy_url)
        proxy_form.addRow(
            QLabel("claude 자식 프로세스 전달 등 실제 동작은 U2에서 제공됩니다.", proxy_tab)
        )
        tabs.addTab(proxy_tab, "프록시")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load_settings()
        self._load_autoreply_toggle()
        if initial_tab == TAB_AUTOREPLY:
            tabs.setCurrentIndex(self._autoreply_tab_index)

    def _load_settings(self) -> None:
        try:
            poll_minutes = self._bridge.call_backend(
                self._api.get_setting("default_poll_interval_min", 5)
            )
            preview_position = self._bridge.call_backend(
                self._api.get_setting("preview_position", "bottom")
            )
            theme = self._bridge.call_backend(self._api.get_setting("theme", "system"))
            autoreply_timeout = self._bridge.call_backend(
                self._api.get_setting("autoreply_timeout_sec", 120)
            )
            proxy_url = self._bridge.call_backend(self._api.get_setting("https_proxy", ""))
        except Exception:  # noqa: BLE001 — 설정 로드 실패 시 기본값 유지
            return
        self._poll_interval.setValue(int(poll_minutes or 5))
        idx = self._preview_position.findData(preview_position)
        self._preview_position.setCurrentIndex(idx if idx >= 0 else 0)
        idx = self._theme_combo.findData(theme)
        self._theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._autoreply_timeout.setValue(int(autoreply_timeout or 120))
        self._proxy_url.setText(proxy_url or "")
        self._load_mcp_settings()
        self._load_update_status()

    # ------------------------------------------------------------ MCP 탭(§11.7, §6.2)

    def _load_mcp_settings(self) -> None:
        try:
            mode = self._bridge.call_backend(self._api.get_setting("mcp.send_mode", "confirm"))
            allowlist = self._bridge.call_backend(self._api.get_setting("mcp.allowlist", []))
            port = self._bridge.call_backend(self._api.get_setting("mcp.port", _DEFAULT_MCP_PORT))
        except Exception:  # noqa: BLE001 — 로드 실패 시 기본값 유지
            return
        idx = self._mcp_send_mode.findData(mode)
        self._mcp_send_mode.setCurrentIndex(idx if idx >= 0 else 0)
        if isinstance(allowlist, list):
            self._mcp_allowlist.setPlainText("\n".join(str(x) for x in allowlist))
        try:
            self._mcp_port.setValue(int(port))
        except (TypeError, ValueError):
            self._mcp_port.setValue(_DEFAULT_MCP_PORT)

    def _mcp_allowlist_entries(self) -> list[str]:
        entries = []
        for line in self._mcp_allowlist.toPlainText().splitlines():
            value = line.strip().lower()
            if value and " " not in value and value not in entries:
                entries.append(value)
        return entries

    # ------------------------------------------------------------ 자동회신 토글(§11.7, §7.0)

    def _load_autoreply_toggle(self) -> None:
        """토글 상태를 (다시) 읽는다. 읽은 `generation`이 켜기 CAS의 기준값이 된다."""
        getter = getattr(self._api, "get_autoreply_toggle", None)
        view: object = None
        if getter is not None:
            try:
                view = self._bridge.call_backend(getter())
            except Exception:  # noqa: BLE001 — 읽지 못하면 토글을 잠근다(fail-closed)
                view = None
        self._autoreply_view = view if isinstance(view, dict) else None
        if self._autoreply_view is None:
            self._autoreply_enabled.setChecked(False)
            self._autoreply_enabled.setEnabled(False)
            self._autoreply_state_label.setText("상태: 확인할 수 없습니다")
            return
        self._autoreply_enabled.setEnabled(True)
        self._autoreply_enabled.setChecked(bool(self._autoreply_view.get("enabled")))
        self._autoreply_state_label.setText(describe_autoreply_state(self._autoreply_view))

    def _confirm_enable(self, preflight: dict | None) -> bool:
        """켜기 확인창을 띄운다(테스트에서 대체할 수 있게 메서드로 분리)."""
        dialog = AutoReplyEnableConfirmDialog(preflight, parent=self)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _apply_autoreply_toggle(self) -> bool:
        """토글 변경을 적용한다. 창을 닫아도 되면 True, 열어 둔 채 다시 결정해야 하면 False."""
        view = self._autoreply_view
        if view is None or not self._autoreply_enabled.isEnabled():
            return True
        loaded = bool(view.get("enabled"))
        wanted = self._autoreply_enabled.isChecked()
        if wanted == loaded:
            return True
        if not wanted:
            # on → off: 확인창 없이 바로(안전한 방향). CAS 없이 항상 성공한다(L-A).
            try:
                result = self._bridge.call_backend(self._api.disable_autoreply(), timeout=15.0)
            except Exception as exc:  # noqa: BLE001
                plain_warning(self, "자동회신 끄기", f"자동회신을 끄지 못했습니다: {exc}")
                self._load_autoreply_toggle()
                return False
            plain_information(
                self,
                "자동회신 끄기",
                "자동회신을 껐습니다.\n"
                f"- 실행 중이던 잡 {int(result.get('cancelled_jobs', 0))}건 취소\n"
                f"- 발송 대기 {int(result.get('reverted_outbox', 0))}건을 초안으로 되돌림\n"
                f"- 이미 전송 중이던 {int(result.get('sending_in_flight', 0))}건은 "
                "취소할 수 없음",
            )
            return True
        # off → on: 확인창(기본 [취소], 1.5초 잠금) 후 CAS.
        expected = view.get("generation")
        if not isinstance(expected, int) or isinstance(expected, bool):
            plain_warning(
                self, "자동회신 켜기", "자동회신 상태 정보가 올바르지 않아 켤 수 없습니다."
            )
            self._load_autoreply_toggle()
            return False
        if not self._confirm_enable(view.get("preflight")):
            self._autoreply_enabled.setChecked(False)
            return False
        try:
            self._bridge.call_backend(self._api.enable_autoreply(expected), timeout=15.0)
        except Exception as exc:  # noqa: BLE001 — CAS 실패(PolicyError) 포함
            plain_warning(self, "자동회신 켜기", f"자동회신을 켜지 못했습니다: {exc}")
            self._load_autoreply_toggle()
            return False
        return True

    # ------------------------------------------------------------ 업데이트 탭(§11.7)

    def _load_update_status(self) -> None:
        getter = getattr(self._api, "get_update_status", None)
        if getter is None:
            self._update_check_now.setEnabled(False)
            self._update_auto_check.setEnabled(False)
            return
        try:
            view = self._bridge.call_backend(getter())
        except Exception:  # noqa: BLE001 — 조회 실패 시 기본 표시 유지
            return
        self._show_update_status(view)
        self._update_auto_loaded = bool(view.get("auto_check"))
        self._update_auto_check.setChecked(self._update_auto_loaded)

    def _show_update_status(self, view: dict | None) -> None:
        view = view or {}
        version = view.get("current_version") or "-"
        if view.get("is_dev_build"):
            version += " (개발 빌드 — 자동 확인 기본 꺼짐)"
        self._update_current_version.setText(version)
        self._update_last_checked.setText(view.get("checked_at") or "-")
        self._update_result.setText(describe_update_status(view))

    def _on_check_update_now(self) -> None:
        checker = getattr(self._api, "check_update_now", None)
        if checker is None:
            return
        self._update_check_now.setEnabled(False)
        self._update_result.setText("확인 중...")
        async_utils.run_async(self._bridge, checker(), self._on_check_update_done, parent=self)

    def _on_check_update_done(self, future) -> None:  # noqa: ANN001 — concurrent.futures.Future
        self._update_check_now.setEnabled(True)
        try:
            view = future.result()
        except Exception as exc:  # noqa: BLE001
            self._update_result.setText(f"확인하지 못했습니다: {exc}")
            return
        self._show_update_status(view)
        parent = self.parent()
        if parent is not None and hasattr(parent, "apply_update_status"):
            parent.apply_update_status(view)

    # ------------------------------------------------------------ 저장

    def _on_theme_changed(self, _index: int) -> None:
        self._apply_theme(self._theme_combo.currentData())

    def _on_save(self) -> None:
        pairs: dict[str, Any] = {
            "default_poll_interval_min": self._poll_interval.value(),
            "preview_position": self._preview_position.currentData(),
            "theme": self._theme_combo.currentData(),
            "autoreply_timeout_sec": self._autoreply_timeout.value(),
            "https_proxy": self._proxy_url.text().strip(),
            "mcp.send_mode": self._mcp_send_mode.currentData(),
            "mcp.allowlist": self._mcp_allowlist_entries(),
            "mcp.port": self._mcp_port.value(),
        }
        try:
            for key, value in pairs.items():
                self._bridge.call_backend(self._api.set_setting(key, value))
            setter = getattr(self._api, "set_update_auto_check", None)
            auto = self._update_auto_check.isChecked()
            # 사용자가 실제로 바꿨을 때만 저장한다(저장값이 없으면 빌드별 기본값을 따르게 둔다).
            if (
                setter is not None
                and self._update_auto_loaded is not None
                and (auto != self._update_auto_loaded)
            ):
                self._bridge.call_backend(setter(auto))
        except Exception:  # noqa: BLE001 — 저장 실패해도 창은 닫되 테마는 즉시 반영한다
            pass
        self._apply_theme(self._theme_combo.currentData())
        # 자동회신 토글은 범용 저장 루프에 넣지 않고 전용 API로만 바꾼다(보호 키, §11.7).
        # 확인창 취소·CAS 실패면 창을 열어 둔다(다시 읽은 상태를 보고 다시 결정하도록).
        if not self._apply_autoreply_toggle():
            return
        self.accept()
