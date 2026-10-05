"""계정 설정 다이얼로그 (DESIGN.md §11.3).

`AccountManagerDialog`(계정 목록 + 추가/편집)와 `AccountEditDialog`(탭: 일반/수신/
송신/자동회신, 프리셋 테이블, [연결 테스트])로 나뉜다. 비밀번호는 keyring에만
저장한다(`UiApi.create_account`/`update_account`가 `secrets.keyring_store`를 거친다).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
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

from emailtomcp.ui import async_utils
from emailtomcp.ui.message_box import plain_warning

GLOBAL_AUTOREPLY_OFF_NOTICE = (
    "전역 자동회신이 꺼져 있어 지금은 적용되지 않습니다(설정 > 자동회신)."
)
TRUST_FIELDS_NOTE = (
    "위 두 값은 즉시발송의 인증 전제조건입니다. 메일 서비스별 실측(어떤 값을 믿을 수 있는지)이 "
    "끝난 서버에서만 입력할 수 있어야 하므로, 다음 업데이트에서 실측 완료 후 제공됩니다. "
    "지금은 입력할 수 없으며, 이번 버전의 자동회신은 모두 초안이라 동작에 영향이 없습니다."
)

# §7.6 ③ ar_capability(R17 실측, mail/ar_presets.py)가 생기기 전까지 신뢰 AR 입력은 닫아 둔다.
TRUST_FIELDS_EDITABLE = False

# DESIGN.md §11.3 프리셋 표. "note"는 안내 문구, "locked_protocol"이 있으면 수신
# 프로토콜 선택을 그 값으로 고정한다. "disabled"는 이 버전에서 선택할 수 없는 행이다.
PRESETS: dict[str, dict[str, Any]] = {
    "gmail": {
        "label": "Gmail",
        "incoming_protocol": "imap",
        "in_host": "imap.gmail.com",
        "in_port": 993,
        "in_security": "ssl",
        "out_host": "smtp.gmail.com",
        "out_port": 465,
        "out_security": "ssl",
        "note": "2단계 인증 + 앱 비밀번호(16자) 필수. 기본 비밀번호 로그인은 차단됩니다.",
    },
    "naver": {
        "label": "네이버",
        "incoming_protocol": "imap",
        "in_host": "imap.naver.com",
        "in_port": 993,
        "in_security": "ssl",
        "out_host": "smtp.naver.com",
        "out_port": 465,
        "out_security": "ssl",
        "note": "2단계 인증 + 앱 비밀번호 필수. 메일 환경설정에서 IMAP/POP3 사용을 켜야 합니다.",
    },
    "daum": {
        "label": "다음",
        "incoming_protocol": "imap",
        "in_host": "imap.daum.net",
        "in_port": 993,
        "in_security": "ssl",
        "out_host": "smtp.daum.net",
        "out_port": 465,
        "out_security": "ssl",
        "note": "2단계 인증 + 앱 비밀번호 필수. 앱비밀번호 관리 위치가 바뀌었을 수 있습니다.",
    },
    "kakao": {
        "label": "카카오",
        "incoming_protocol": "imap",
        "in_host": "imap.kakao.com",
        "in_port": 993,
        "in_security": "ssl",
        "out_host": "smtp.kakao.com",
        "out_port": 465,
        "out_security": "ssl",
        "note": "2단계 인증 + 앱 비밀번호 필수. 카카오메일 설정에서 IMAP/POP3 사용을 켜야 합니다.",
    },
    "hiworks": {
        "label": "하이웍스",
        "incoming_protocol": "pop3",
        "in_host": "pop3s.hiworks.com",
        "in_port": 995,
        "in_security": "ssl",
        "out_host": "smtps.hiworks.com",
        "out_port": 465,
        "out_security": "ssl",
        "locked_protocol": "pop3",
        "note": "POP3 전용(IMAP 미지원). 조직 관리자가 POP3/SMTP 사용을 허용해야 하며, "
        "서버 폴더·읽음 동기화가 되지 않습니다(로컬 전용).",
    },
    "m365": {
        "label": "Microsoft 365 / Outlook.com",
        "disabled": True,
        "note": "OAuth2 필수(IMAP/POP 기본 인증 차단). 이 버전은 미지원입니다(P4 예정).",
    },
    "custom": {
        "label": "사용자 지정",
        "incoming_protocol": "imap",
        "in_host": "",
        "in_port": 993,
        "in_security": "ssl",
        "out_host": "",
        "out_port": 465,
        "out_security": "ssl",
        "note": "호스트/포트/보안을 직접 입력합니다.",
    },
}

_SECURITY_OPTIONS = (("ssl", "SSL"), ("starttls", "STARTTLS"), ("none", "암호화 없음"))


class AccountManagerDialog(QDialog):
    """도구 > 계정 — 계정 목록 + [추가]/[편집]."""

    def __init__(self, *, api: Any, bridge: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("계정")
        self.resize(480, 360)
        self._api = api
        self._bridge = bridge

        layout = QVBoxLayout(self)
        self._list = QListWidget(self)
        self._list.itemDoubleClicked.connect(self._on_edit)
        layout.addWidget(self._list)

        button_row = QHBoxLayout()
        add_btn = QPushButton("추가", self)
        add_btn.setProperty("variant", "primary")
        add_btn.clicked.connect(self._on_add)
        edit_btn = QPushButton("편집", self)
        edit_btn.clicked.connect(self._on_edit)
        close_btn = QPushButton("닫기", self)
        close_btn.clicked.connect(self.accept)
        for btn in (add_btn, edit_btn, close_btn):
            button_row.addWidget(btn)
        layout.addLayout(button_row)

        self._reload()

    def _reload(self) -> None:
        self._list.clear()
        try:
            accounts = self._bridge.call_backend(self._api.list_accounts(), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "계정", f"계정 목록을 불러오지 못했습니다: {exc}")
            return
        for account in accounts:
            item = QListWidgetItem(f"{account.display_name}  <{account.email_address}>")
            item.setData(Qt.ItemDataRole.UserRole, account.id)
            self._list.addItem(item)

    def _on_add(self) -> None:
        dialog = AccountEditDialog(account_id=None, api=self._api, bridge=self._bridge, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload()

    def _on_edit(self, *_args: object) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        account_id = item.data(Qt.ItemDataRole.UserRole)
        dialog = AccountEditDialog(
            account_id=account_id, api=self._api, bridge=self._bridge, parent=self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload()


class AccountEditDialog(QDialog):
    """계정 추가/편집 — 탭(일반/수신/송신/자동회신) + 프리셋 테이블 + [연결 테스트]."""

    def __init__(
        self, *, account_id: int | None, api: Any, bridge: Any, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._account_id = account_id
        self._api = api
        self._bridge = bridge
        self._is_new = account_id is None
        self.setWindowTitle("계정 추가" if self._is_new else "계정 편집")
        self.resize(560, 520)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_preset_table())

        tabs = QTabWidget(self)
        tabs.addTab(self._build_general_tab(), "일반")
        tabs.addTab(self._build_incoming_tab(), "수신")
        tabs.addTab(self._build_outgoing_tab(), "송신")
        tabs.addTab(self._build_autoreply_tab(), "자동회신")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self._is_new:
            self._email.setReadOnly(False)
        else:
            self._email.setReadOnly(True)  # 이메일 변경은 지원하지 않는다(식별자 성격)
            self._load_existing()

    # --- 프리셋 테이블 -----------------------------------------------------

    def _build_preset_table(self) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("프리셋 선택(호스트/포트/보안을 자동으로 채웁니다)", box))

        self._preset_table = QTableWidget(len(PRESETS), 2, box)
        self._preset_table.setHorizontalHeaderLabels(["프리셋", "안내"])
        self._preset_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._preset_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._preset_table.verticalHeader().setVisible(False)
        self._preset_table.setMaximumHeight(140)

        self._preset_keys: list[str] = list(PRESETS.keys())
        for row, key in enumerate(self._preset_keys):
            preset = PRESETS[key]
            label_item = QTableWidgetItem(preset["label"])
            note_item = QTableWidgetItem(preset.get("note", ""))
            if preset.get("disabled"):
                for item in (label_item, note_item):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._preset_table.setItem(row, 0, label_item)
            self._preset_table.setItem(row, 1, note_item)
        self._preset_table.resizeColumnToContents(0)
        self._preset_table.horizontalHeader().setStretchLastSection(True)
        self._preset_table.itemSelectionChanged.connect(self._on_preset_selected)
        layout.addWidget(self._preset_table)
        return box

    def _on_preset_selected(self) -> None:
        row = self._preset_table.currentRow()
        if row < 0:
            return
        key = self._preset_keys[row]
        preset = PRESETS[key]
        if preset.get("disabled"):
            note = preset.get("note", "현재 버전에서 지원하지 않습니다.")
            QMessageBox.information(self, "지원 예정", note)
            return
        self._provider_preset = key
        protocol_label = "POP3" if preset["incoming_protocol"] == "pop3" else "IMAP"
        self._protocol_combo.setCurrentText(protocol_label)
        self._protocol_combo.setEnabled("locked_protocol" not in preset)
        self._in_host.setText(preset["in_host"])
        self._in_port.setValue(preset["in_port"])
        self._in_security.setCurrentIndex(
            [v for v, _ in _SECURITY_OPTIONS].index(preset["in_security"])
        )
        self._out_host.setText(preset["out_host"])
        self._out_port.setValue(preset["out_port"])
        self._out_security.setCurrentIndex(
            [v for v, _ in _SECURITY_OPTIONS].index(preset["out_security"])
        )

    # --- 일반 탭 -----------------------------------------------------------

    def _build_general_tab(self) -> QWidget:
        tab = QWidget(self)
        form = QFormLayout(tab)
        self._display_name = QLineEdit(tab)
        form.addRow("표시 이름", self._display_name)
        self._email = QLineEdit(tab)
        form.addRow("이메일 주소", self._email)
        self._sender_name = QLineEdit(tab)
        form.addRow("보내는 사람 이름", self._sender_name)
        self._signature = QLineEdit(tab)
        self._signature.setPlaceholderText("서명(평문)")
        form.addRow("서명", self._signature)
        self._poll_interval = QSpinBox(tab)
        self._poll_interval.setRange(1, 1440)
        self._poll_interval.setSuffix(" 분")
        self._poll_interval.setValue(5)
        form.addRow("수신 주기", self._poll_interval)
        self._provider_preset = "custom"
        return tab

    # --- 수신 탭 -----------------------------------------------------------

    def _build_incoming_tab(self) -> QWidget:
        tab = QWidget(self)
        form = QFormLayout(tab)
        self._protocol_combo = QComboBox(tab)
        self._protocol_combo.addItems(["IMAP", "POP3"])
        form.addRow("프로토콜", self._protocol_combo)
        self._in_host = QLineEdit(tab)
        form.addRow("호스트", self._in_host)
        self._in_port = QSpinBox(tab)
        self._in_port.setRange(1, 65535)
        self._in_port.setValue(993)
        form.addRow("포트", self._in_port)
        self._in_security = QComboBox(tab)
        for value, label in _SECURITY_OPTIONS:
            self._in_security.addItem(label, value)
        form.addRow("보안", self._in_security)
        self._in_username = QLineEdit(tab)
        form.addRow("사용자명", self._in_username)
        self._in_password = QLineEdit(tab)
        self._in_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._in_password.setPlaceholderText(
            "" if self._is_new else "변경하지 않으면 기존 비밀번호 유지"
        )
        form.addRow("비밀번호", self._in_password)

        test_row = QHBoxLayout()
        self._in_test_btn = QPushButton("연결 테스트", tab)
        self._in_test_btn.clicked.connect(self._on_test_incoming)
        self._in_test_label = QLabel("", tab)
        # 메일 서버 응답 문구가 그대로 표시되므로 리치텍스트로 해석하지 않는다(보안검토 H-3).
        self._in_test_label.setTextFormat(Qt.TextFormat.PlainText)
        test_row.addWidget(self._in_test_btn)
        test_row.addWidget(self._in_test_label)
        form.addRow(test_row)
        return tab

    # --- 송신 탭 -----------------------------------------------------------

    def _build_outgoing_tab(self) -> QWidget:
        tab = QWidget(self)
        form = QFormLayout(tab)
        self._out_host = QLineEdit(tab)
        form.addRow("호스트", self._out_host)
        self._out_port = QSpinBox(tab)
        self._out_port.setRange(1, 65535)
        self._out_port.setValue(465)
        form.addRow("포트", self._out_port)
        self._out_security = QComboBox(tab)
        for value, label in _SECURITY_OPTIONS:
            self._out_security.addItem(label, value)
        form.addRow("보안", self._out_security)
        self._out_username = QLineEdit(tab)
        self._out_username.setPlaceholderText("비워두면 수신 사용자명과 동일")
        form.addRow("사용자명(다를 때만)", self._out_username)

        test_row = QHBoxLayout()
        self._out_test_btn = QPushButton("연결 테스트", tab)
        self._out_test_btn.clicked.connect(self._on_test_outgoing)
        self._out_test_label = QLabel("", tab)
        # 메일 서버 응답 문구가 그대로 표시되므로 리치텍스트로 해석하지 않는다(보안검토 H-3).
        self._out_test_label.setTextFormat(Qt.TextFormat.PlainText)
        test_row.addWidget(self._out_test_btn)
        test_row.addWidget(self._out_test_label)
        form.addRow(test_row)
        return tab

    # --- 자동회신 탭 ---------------------------------------------------------

    def _build_autoreply_tab(self) -> QWidget:
        tab = QWidget(self)
        form = QFormLayout(tab)
        self._auto_reply_enabled = QCheckBox("이 계정에 자동회신 규칙 적용", tab)
        form.addRow("", self._auto_reply_enabled)
        # §11.3: 전역 토글이 꺼져 있으면 안내만 하고 체크박스는 계속 편집할 수 있다.
        self._global_off_label = QLabel(tab)
        self._global_off_label.setTextFormat(Qt.TextFormat.PlainText)
        self._global_off_label.setWordWrap(True)
        self._global_off_label.setText(GLOBAL_AUTOREPLY_OFF_NOTICE)
        self._global_off_label.setProperty("role", "previewMeta")
        self._global_off_label.setVisible(not self._global_autoreply_enabled())
        form.addRow("", self._global_off_label)
        self._aliases = QLineEdit(tab)
        self._aliases.setPlaceholderText("쉼표로 구분된 별칭 주소")
        form.addRow("내 별칭", self._aliases)
        # §7.6 ③: ar_capability=confirmed일 때만 자동 탐지·수동 입력을 연다. ar_capability는
        # R17 실측(mail/ar_presets.py) 뒤에 계산되므로 지금은 언제나 비활성이다(데카르트 31번 M-4).
        # 기존에 저장된 값이 있으면 보여 주기만 하고, 저장할 때 이 두 값은 보내지 않는다.
        self._trusted_authserv_id = QLineEdit(tab)
        self._trusted_authserv_id.setPlaceholderText("다음 업데이트에서 제공")
        self._trusted_authserv_id.setEnabled(TRUST_FIELDS_EDITABLE)
        form.addRow("신뢰 Authentication-Results ID", self._trusted_authserv_id)
        self._trusted_boundary_by = QLineEdit(tab)
        self._trusted_boundary_by.setPlaceholderText("다음 업데이트에서 제공")
        self._trusted_boundary_by.setEnabled(TRUST_FIELDS_EDITABLE)
        form.addRow("수신 경계 서버(Received by)", self._trusted_boundary_by)
        trust_note = QLabel(tab)
        trust_note.setTextFormat(Qt.TextFormat.PlainText)
        trust_note.setWordWrap(True)
        trust_note.setText(TRUST_FIELDS_NOTE)
        trust_note.setProperty("role", "previewMeta")
        form.addRow("", trust_note)
        return tab

    def _global_autoreply_enabled(self) -> bool:
        getter = getattr(self._api, "get_autoreply_toggle", None)
        if getter is None or self._bridge is None:
            return False
        try:
            view = self._bridge.call_backend(getter(), timeout=5.0)
        except Exception:  # noqa: BLE001 — 읽지 못하면 꺼짐으로 안내(fail-closed)
            return False
        return bool(isinstance(view, dict) and view.get("enabled"))

    # --- 로드/저장 ---------------------------------------------------------

    def _load_existing(self) -> None:
        try:
            account = self._bridge.call_backend(
                self._api.get_account(self._account_id), timeout=5.0
            )
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "계정 편집", f"계정을 불러오지 못했습니다: {exc}")
            return
        if account is None:
            return
        self._display_name.setText(account.display_name)
        self._email.setText(account.email_address)
        self._sender_name.setText(account.sender_name or "")
        self._signature.setText(account.signature or "")
        self._poll_interval.setValue(max(1, account.poll_interval_sec // 60))
        protocol_label = "POP3" if account.incoming_protocol == "pop3" else "IMAP"
        self._protocol_combo.setCurrentText(protocol_label)
        self._in_host.setText(account.in_host)
        self._in_port.setValue(account.in_port)
        self._in_security.setCurrentIndex(
            [v for v, _ in _SECURITY_OPTIONS].index(account.in_security)
        )
        self._in_username.setText(account.in_username)
        self._out_host.setText(account.out_host)
        self._out_port.setValue(account.out_port)
        self._out_security.setCurrentIndex(
            [v for v, _ in _SECURITY_OPTIONS].index(account.out_security)
        )
        out_username = "" if account.out_auth_same_as_in else (account.out_username or "")
        self._out_username.setText(out_username)
        self._trusted_authserv_id.setText(account.trusted_authserv_id or "")
        self._trusted_boundary_by.setText(getattr(account, "trusted_boundary_by", None) or "")
        self._auto_reply_enabled.setChecked(bool(account.auto_reply_enabled))
        self._aliases.setText(", ".join(account.aliases))
        self._provider_preset = account.provider_preset or "custom"

    def _collect_fields(self) -> dict[str, Any]:
        out_username = self._out_username.text().strip()
        return {
            "display_name": self._display_name.text().strip() or self._email.text().strip(),
            "email_address": self._email.text().strip(),
            "sender_name": self._sender_name.text().strip() or None,
            "signature": self._signature.text().strip() or None,
            "provider_preset": self._provider_preset,
            "poll_interval_sec": self._poll_interval.value() * 60,
            "incoming_protocol": "pop3" if self._protocol_combo.currentText() == "POP3" else "imap",
            "in_host": self._in_host.text().strip(),
            "in_port": self._in_port.value(),
            "in_security": self._in_security.currentData(),
            "in_username": self._in_username.text().strip(),
            "out_host": self._out_host.text().strip(),
            "out_port": self._out_port.value(),
            "out_security": self._out_security.currentData(),
            "out_username": out_username or None,
            "out_auth_same_as_in": not out_username,
            "auto_reply_enabled": self._auto_reply_enabled.isChecked(),
            "aliases": [a.strip() for a in self._aliases.text().split(",") if a.strip()],
            **(
                {
                    "trusted_authserv_id": self._trusted_authserv_id.text().strip() or None,
                    "trusted_boundary_by": self._trusted_boundary_by.text().strip() or None,
                }
                if TRUST_FIELDS_EDITABLE
                else {}
            ),
        }

    def _on_save(self) -> None:
        fields = self._collect_fields()
        if not fields["email_address"] or not fields["in_host"] or not fields["out_host"]:
            QMessageBox.warning(self, "계정 저장", "이메일 주소/수신·송신 호스트는 필수입니다.")
            return
        password = self._in_password.text() or None
        try:
            if self._is_new:
                if not password:
                    QMessageBox.warning(self, "계정 저장", "새 계정은 비밀번호를 입력해야 합니다.")
                    return
                self._bridge.call_backend(self._api.create_account(fields, password), timeout=5.0)
            else:
                fields.pop("email_address", None)  # 이메일은 변경 불가(식별자 성격)
                self._bridge.call_backend(
                    self._api.update_account(self._account_id, fields, password), timeout=5.0
                )
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "계정 저장", f"저장하지 못했습니다: {exc}")
            return
        self.accept()

    # --- 연결 테스트(네트워크 I/O — Qt 메인 스레드를 막지 않는 비동기 경로) --------

    def _on_test_incoming(self) -> None:
        fields = self._collect_fields()
        password = self._in_password.text()
        if not password:
            self._in_test_label.setText("비밀번호를 입력하세요")
            return
        self._in_test_label.setText("테스트 중...")
        self._in_test_btn.setEnabled(False)

        def _done(future) -> None:  # noqa: ANN001
            self._in_test_btn.setEnabled(True)
            try:
                ok, message = future.result()
            except Exception as exc:  # noqa: BLE001
                self._in_test_label.setText(f"오류: {exc}")
                return
            self._in_test_label.setText(message if ok else f"실패: {message}")

        async_utils.run_async(
            self._bridge, self._api.test_incoming_connection(fields, password), _done, parent=self
        )

    def _on_test_outgoing(self) -> None:
        fields = self._collect_fields()
        password = self._in_password.text() or None
        self._out_test_label.setText("테스트 중...")
        self._out_test_btn.setEnabled(False)

        def _done(future) -> None:  # noqa: ANN001
            self._out_test_btn.setEnabled(True)
            try:
                ok, message = future.result()
            except Exception as exc:  # noqa: BLE001
                self._out_test_label.setText(f"오류: {exc}")
                return
            self._out_test_label.setText(message if ok else f"실패: {message}")

        async_utils.run_async(
            self._bridge, self._api.test_outgoing_connection(fields, password), _done, parent=self
        )
