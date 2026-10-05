"""규칙 설정 화면 (DESIGN.md §11.4, §7.0·§7.1) — P3 Phase B.

- `RuleManagerDialog`: 규칙 목록(순서·사용·이름·계정·동작·응답 방식), [추가]/[편집]/[삭제]/[▲]/[▼],
  "규칙에 맞지 않는 메일" 처리(무시/초안), 전역 토글 off 배너.
- `RuleEditDialog`: 이름·계정·사용, 조건(모두/하나라도, 항목·비교·값), 동작, 응답 방식(Claude 생성/
  고정 템플릿), 추가 지침, 최대 글자 수, [드라이런](claude 미실행, 최근 메일에 적용해 보기).

이번 단계(Phase B)의 동작 선택지는 **무시·초안 만들기**뿐이다. **즉시발송(auto_send)** 항목은
목록에 보이되 비활성이고, 툴팁·안내문으로 "다음 업데이트에서 제공 예정"을 알린다(숨기지 않음).

비신뢰 문자열(드라이런 표의 제목·보낸사람, 안내 라벨)은 모두 평문이다 — QTableWidgetItem은
리치텍스트로 해석하지 않고, QLabel은 PlainText로 고정한다(§7.10 H-3·N-2·N-5).
UI는 rules 패키지를 import하지 않는다(§4.2) — 저장 검증은 백엔드(`UiApi.save_rule`)가 한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from emailtomcp.ui.dialogs.manual_viewer import ManualViewer
from emailtomcp.ui.message_box import plain_information, plain_warning

logger = logging.getLogger(__name__)

AUTOREPLY_OFF_BANNER = "자동회신이 꺼져 있습니다. 규칙은 저장되지만 적용되지 않습니다."
AUTOSEND_DISABLED_TOOLTIP = (
    "즉시발송은 다음 업데이트에서 제공될 예정입니다. 지금은 '초안 만들기'만 쓸 수 있습니다."
)
AUTOSEND_NOTICE = (
    "즉시발송(사람 확인 없이 바로 보내기)은 다음 업데이트에서 제공될 예정입니다. "
    "이번 버전에서는 자동회신 결과가 모두 '초안'으로 저장되며, 직접 검토한 뒤 보낼 수 있습니다."
)
RE2_GUIDE = (
    "정규식은 RE2 문법입니다(대소문자 무시, 200자 이하). 역참조(\\1)와 전후방탐색((?=…))은 "
    "쓸 수 없습니다. 본문은 앞 10,000자만 검사합니다."
)

ACTION_CHOICES: tuple[tuple[str, str, bool], ...] = (
    ("ignore", "무시(답장하지 않음)", True),
    ("draft", "초안 만들기(직접 검토 후 발송)", True),
    ("auto_send", "즉시발송 — 다음 업데이트에서 제공 예정", False),
)
ACTION_LABELS = {"ignore": "무시", "draft": "초안", "auto_send": "즉시발송(초안으로 동작)"}
REPLY_MODE_LABELS = {"generated": "Claude 생성", "fixed_template": "고정 템플릿"}

FIELD_LABELS: dict[str, str] = {
    "from_address": "보낸사람 주소",
    "from_domain": "보낸사람 도메인",
    "to": "받는사람",
    "cc": "참조",
    "subject": "제목",
    "body": "본문",
    "has_attachment": "첨부 있음",
    "received_hour": "받은 시각(시)",
    "received_weekday": "받은 요일(0=월~6=일)",
}
_TEXT_OPS = ("contains", "not_contains", "equals", "starts_with", "ends_with", "regex")
_ADDR_OPS = ("in_list", *_TEXT_OPS)
FIELD_OPS: dict[str, tuple[str, ...]] = {
    "from_address": _ADDR_OPS,
    "from_domain": _ADDR_OPS,
    "to": _ADDR_OPS,
    "cc": _ADDR_OPS,
    "subject": _TEXT_OPS,
    "body": _TEXT_OPS,
    "has_attachment": ("equals",),
    "received_hour": ("between", "equals"),
    "received_weekday": ("between", "equals"),
}
OP_LABELS: dict[str, str] = {
    "equals": "같음",
    "contains": "포함",
    "not_contains": "포함하지 않음",
    "starts_with": "~로 시작",
    "ends_with": "~로 끝남",
    "regex": "정규식(RE2)",
    "in_list": "목록 중 하나(쉼표 구분)",
    "between": "범위(시작-끝, 끝 미포함)",
}
RESULT_LABELS = {
    "draft": "초안 예정",
    "ignore": "무시",
    "blocked": "차단(LoopGuard)",
    "no_match": "맞지 않음",
}


def _plain_label(text: str, parent: QWidget | None = None) -> QLabel:
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setWordWrap(True)
    return label


def value_to_text(field: str, op: str, value: Any) -> str:
    if op == "in_list" and isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if op == "between" and isinstance(value, list) and len(value) == 2:
        return f"{value[0]}-{value[1]}"
    if field == "has_attachment":
        return "예" if value else "아니오"
    return "" if value is None else str(value)


def text_to_value(field: str, op: str, text: str) -> Any:
    """편집 값을 조건 값으로 바꾼다. 형식이 틀리면 ValueError(한국어)."""
    text = text.strip()
    if op == "in_list":
        items = [t.strip() for t in text.replace("\n", ",").split(",") if t.strip()]
        if not items:
            raise ValueError("목록 값을 쉼표로 구분해 입력하세요")
        return items
    if op == "between":
        parts = [p.strip() for p in text.split("-")]
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("범위는 '시작-끝' 형식의 숫자로 입력하세요(예: 9-18)")
        return [int(parts[0]), int(parts[1])]
    if field == "has_attachment":
        if text in ("예", "true", "True", "1", "있음"):
            return True
        if text in ("아니오", "false", "False", "0", "없음"):
            return False
        raise ValueError("첨부 있음은 '예' 또는 '아니오'로 입력하세요")
    if field in ("received_hour", "received_weekday"):
        if not text.isdigit():
            raise ValueError("시각·요일은 숫자로 입력하세요")
        return int(text)
    if not text:
        raise ValueError("조건 값을 입력하세요")
    return text


class RuleEditDialog(QDialog):
    """규칙 한 개 추가/편집 + 드라이런."""

    def __init__(
        self,
        *,
        api: Any,
        bridge: Any,
        rule: dict | None,
        accounts: list[tuple[int, str]],
        autoreply_enabled: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._api = api
        self._bridge = bridge
        self._rule = rule
        self.setWindowTitle("규칙 편집" if rule else "규칙 추가")
        self.resize(860, 760)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(self)
        self.name_edit.setMaxLength(100)
        form.addRow("이름", self.name_edit)
        self.account_combo = QComboBox(self)
        self.account_combo.addItem("모든 계정", None)
        for account_id, label in accounts:
            self.account_combo.addItem(label, account_id)
        form.addRow("적용 계정", self.account_combo)
        self.enabled_check = QCheckBox("이 규칙 사용", self)
        self.enabled_check.setChecked(True)
        form.addRow("", self.enabled_check)
        layout.addLayout(form)

        # ---- 조건
        cond_header = QHBoxLayout()
        cond_header.addWidget(QLabel("조건", self))
        self.match_combo = QComboBox(self)
        self.match_combo.addItem("모두 만족(AND)", "all")
        self.match_combo.addItem("하나라도 만족(OR)", "any")
        cond_header.addWidget(self.match_combo)
        cond_header.addStretch(1)
        add_cond = QPushButton("조건 추가", self)
        add_cond.setProperty("variant", "secondary")
        add_cond.clicked.connect(lambda: self.add_condition_row())
        remove_cond = QPushButton("조건 삭제", self)
        remove_cond.setProperty("variant", "secondary")
        remove_cond.clicked.connect(self._remove_condition_row)
        cond_header.addWidget(add_cond)
        cond_header.addWidget(remove_cond)
        layout.addLayout(cond_header)
        self.cond_table = QTableWidget(0, 3, self)
        self.cond_table.setHorizontalHeaderLabels(["항목", "비교", "값"])
        self.cond_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.cond_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cond_table.setMinimumHeight(130)
        layout.addWidget(self.cond_table)
        guide = _plain_label(RE2_GUIDE + " 조건이 없으면 받은 모든 메일에 맞습니다.", self)
        guide.setProperty("role", "previewMeta")
        layout.addWidget(guide)

        # ---- 동작·응답 방식
        form2 = QFormLayout()
        self.action_combo = QComboBox(self)
        for value, label, _enabled in ACTION_CHOICES:
            self.action_combo.addItem(label, value)
        model = self.action_combo.model()
        for index, (_value, _label, enabled) in enumerate(ACTION_CHOICES):
            if not enabled and isinstance(model, QStandardItemModel):
                item = model.item(index)
                item.setEnabled(False)
                item.setToolTip(AUTOSEND_DISABLED_TOOLTIP)
        self.action_combo.setCurrentIndex(1)  # 기본: 초안 만들기
        form2.addRow("동작", self.action_combo)
        self.autosend_notice = _plain_label(AUTOSEND_NOTICE, self)
        self.autosend_notice.setProperty("role", "previewMeta")
        form2.addRow("", self.autosend_notice)
        self.reply_mode_combo = QComboBox(self)
        self.reply_mode_combo.addItem("Claude가 작성(claude CLI 필요)", "generated")
        self.reply_mode_combo.addItem("고정 템플릿(Claude 미사용)", "fixed_template")
        self.reply_mode_combo.currentIndexChanged.connect(self._on_reply_mode_changed)
        form2.addRow("응답 방식", self.reply_mode_combo)
        self.template_edit = QPlainTextEdit(self)
        self.template_edit.setPlaceholderText(
            "자리표시자: {sender_name_safe} {received_date} {account_name}"
        )
        self.template_edit.setMaximumHeight(90)
        form2.addRow("고정 템플릿", self.template_edit)
        self.instructions_edit = QPlainTextEdit(self)
        self.instructions_edit.setPlaceholderText("Claude에게 줄 추가 지침(선택, 1000자 이하)")
        self.instructions_edit.setMaximumHeight(70)
        form2.addRow("추가 지침", self.instructions_edit)
        self.max_chars_spin = QSpinBox(self)
        self.max_chars_spin.setRange(50, 2000)
        self.max_chars_spin.setValue(400)
        form2.addRow("최대 글자 수", self.max_chars_spin)
        layout.addLayout(form2)

        # ---- 드라이런
        dry_row = QHBoxLayout()
        self.dry_run_button = QPushButton("드라이런(최근 50건에 적용해 보기)", self)
        self.dry_run_button.setProperty("variant", "secondary")
        self.dry_run_button.clicked.connect(self.run_dry_run)
        dry_row.addWidget(self.dry_run_button)
        dry_row.addStretch(1)
        layout.addLayout(dry_row)
        self.dry_banner = _plain_label(
            AUTOREPLY_OFF_BANNER
            + " 드라이런은 꺼져 있어도 할 수 있습니다(claude는 실행하지 않습니다).",
            self,
        )
        self.dry_banner.setProperty("badge", "external")
        self.dry_banner.setVisible(not autoreply_enabled)
        layout.addWidget(self.dry_banner)
        self.dry_summary = _plain_label("", self)
        layout.addWidget(self.dry_summary)
        self.dry_table = QTableWidget(0, 5, self)
        self.dry_table.setHorizontalHeaderLabels(["받은 시각", "보낸사람", "제목", "규칙", "결과"])
        self.dry_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dry_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.dry_table.setMinimumHeight(150)
        layout.addWidget(self.dry_table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if rule:
            self._load(rule)
        else:
            self.add_condition_row("from_domain", "in_list", "")
        self._on_reply_mode_changed()

    # ------------------------------------------------------------ 조건 표

    def add_condition_row(
        self, field: str = "subject", op: str | None = None, value_text: str = ""
    ) -> None:
        row = self.cond_table.rowCount()
        self.cond_table.insertRow(row)
        field_combo = QComboBox(self.cond_table)
        for key, label in FIELD_LABELS.items():
            field_combo.addItem(label, key)
        op_combo = QComboBox(self.cond_table)
        value_edit = QLineEdit(self.cond_table)
        value_edit.setText(value_text)
        self.cond_table.setCellWidget(row, 0, field_combo)
        self.cond_table.setCellWidget(row, 1, op_combo)
        self.cond_table.setCellWidget(row, 2, value_edit)

        def _fill_ops() -> None:
            current = op_combo.currentData()
            op_combo.clear()
            for key in FIELD_OPS.get(field_combo.currentData(), ()):
                op_combo.addItem(OP_LABELS[key], key)
            index = op_combo.findData(current)
            if index >= 0:
                op_combo.setCurrentIndex(index)

        field_combo.currentIndexChanged.connect(lambda _i: _fill_ops())
        index = field_combo.findData(field if field in FIELD_LABELS else "subject")
        field_combo.setCurrentIndex(max(index, 0))
        _fill_ops()
        if op is not None:
            op_index = op_combo.findData(op)
            if op_index >= 0:
                op_combo.setCurrentIndex(op_index)

    def _remove_condition_row(self) -> None:
        row = self.cond_table.currentRow()
        if row < 0:
            row = self.cond_table.rowCount() - 1
        if row >= 0:
            self.cond_table.removeRow(row)

    def conditions(self) -> dict:
        """편집 중인 조건을 백엔드 형식으로. 값 형식이 틀리면 ValueError."""
        items: list[dict] = []
        for row in range(self.cond_table.rowCount()):
            field_combo = self.cond_table.cellWidget(row, 0)
            op_combo = self.cond_table.cellWidget(row, 1)
            value_edit = self.cond_table.cellWidget(row, 2)
            assert isinstance(field_combo, QComboBox) and isinstance(op_combo, QComboBox)
            assert isinstance(value_edit, QLineEdit)
            field = field_combo.currentData()
            op = op_combo.currentData()
            try:
                value = text_to_value(field, op, value_edit.text())
            except ValueError as exc:
                raise ValueError(f"조건 {row + 1}: {exc}") from exc
            items.append({"field": field, "op": op, "value": value})
        return {"match": self.match_combo.currentData(), "conditions": items}

    # ------------------------------------------------------------ 값

    def _on_reply_mode_changed(self, _index: int = 0) -> None:
        fixed = self.reply_mode_combo.currentData() == "fixed_template"
        self.template_edit.setEnabled(fixed)
        self.instructions_edit.setEnabled(not fixed)

    def _load(self, rule: dict) -> None:
        self.name_edit.setText(rule.get("name") or "")
        index = self.account_combo.findData(rule.get("account_id"))
        self.account_combo.setCurrentIndex(max(index, 0))
        self.enabled_check.setChecked(bool(rule.get("enabled", True)))
        conditions = rule.get("conditions") or {}
        match_index = self.match_combo.findData(conditions.get("match", "all"))
        self.match_combo.setCurrentIndex(max(match_index, 0))
        for cond in conditions.get("conditions", []):
            field = cond.get("field", "subject")
            op = cond.get("op", "contains")
            self.add_condition_row(field, op, value_to_text(field, op, cond.get("value")))
        action = rule.get("action", "draft")
        if action == "auto_send":
            # 저장돼 있던 즉시발송 규칙은 이번 버전에서 초안으로 동작한다 — 저장하려면 바꿔야 한다.
            self.action_combo.setCurrentIndex(self.action_combo.findData("draft"))
            self.autosend_notice.setText(
                "이 규칙은 즉시발송으로 저장돼 있지만 이번 버전에서는 초안으로 동작합니다. "
                "저장하면 '초안 만들기'로 바뀝니다. " + AUTOSEND_NOTICE
            )
        else:
            self.action_combo.setCurrentIndex(max(self.action_combo.findData(action), 0))
        mode_index = self.reply_mode_combo.findData(rule.get("reply_mode", "generated"))
        self.reply_mode_combo.setCurrentIndex(max(mode_index, 0))
        self.template_edit.setPlainText(rule.get("fixed_template") or "")
        self.instructions_edit.setPlainText(rule.get("extra_instructions") or "")
        self.max_chars_spin.setValue(int(rule.get("max_chars") or 400))

    def values(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "account_id": self.account_combo.currentData(),
            "enabled": self.enabled_check.isChecked(),
            "conditions": self.conditions(),
            "action": self.action_combo.currentData(),
            "reply_mode": self.reply_mode_combo.currentData(),
            "fixed_template": self.template_edit.toPlainText() or None,
            "extra_instructions": self.instructions_edit.toPlainText() or None,
            "max_chars": self.max_chars_spin.value(),
            "allow_thread_context": False,
            "accept_aligned_dkim": False,
            "cooldown_hours": (self._rule or {}).get("cooldown_hours"),
        }

    # ------------------------------------------------------------ 드라이런·저장

    def run_dry_run(self) -> None:
        try:
            data = self.values()
        except ValueError as exc:
            plain_warning(self, "드라이런", str(exc))
            return
        try:
            report = self._bridge.call_backend(self._api.dry_run_rule(data, 50), timeout=20.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "드라이런", f"드라이런을 하지 못했습니다: {exc}")
            return
        self.show_dry_run(report)

    def show_dry_run(self, report: dict) -> None:
        self.dry_summary.setText(
            f"최근 {report.get('total', 0)}건 중 규칙에 맞음 {report.get('matched', 0)}건, "
            f"LoopGuard 차단 {report.get('blocked', 0)}건, "
            f"초안 예정 {report.get('would_draft', 0)}건 (claude는 실행하지 않았습니다)"
        )
        rows = report.get("rows", [])
        self.dry_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            result = RESULT_LABELS.get(row.get("result"), str(row.get("result")))
            if row.get("loop_reasons"):
                result += " — " + ", ".join(row["loop_reasons"])
            values = [
                str(row.get("received_at") or "")[:19],
                str(row.get("from_addr") or ""),
                str(row.get("subject") or ""),
                "맞음" if row.get("matched") else "",
                result,
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)  # 평문 셀(리치텍스트 해석 없음)
                if c == 4 and row.get("note"):
                    item.setToolTip(str(row["note"]).replace("<", "&lt;"))
                self.dry_table.setItem(r, c, item)

    def _on_save(self) -> None:
        try:
            data = self.values()
        except ValueError as exc:
            plain_warning(self, "규칙 저장", str(exc))
            return
        rule_id = self._rule.get("id") if self._rule else None
        version = self._rule.get("version") if self._rule else None
        try:
            result = self._bridge.call_backend(
                self._api.save_rule(rule_id, data, version), timeout=10.0
            )
        except Exception as exc:  # noqa: BLE001 — PolicyError 등은 평문으로 보여 준다
            plain_warning(self, "규칙 저장", f"저장하지 못했습니다: {exc}")
            return
        warnings = (result or {}).get("warnings") or []
        if warnings:
            plain_information(self, "규칙 저장", "저장했습니다.\n\n" + "\n".join(warnings))
        self.accept()


class RuleManagerDialog(QDialog):
    """규칙 목록과 전역 규칙 설정(§11.4)."""

    def __init__(
        self,
        *,
        api: Any,
        bridge: Any,
        open_settings: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._api = api
        self._bridge = bridge
        self._open_settings = open_settings
        self._rules: list[dict] = []
        self._autoreply_enabled = False
        self.setWindowTitle("규칙 설정")
        self.resize(820, 520)
        layout = QVBoxLayout(self)

        banner_row = QHBoxLayout()
        self.banner = _plain_label(AUTOREPLY_OFF_BANNER, self)
        self.banner.setProperty("badge", "external")
        banner_row.addWidget(self.banner, stretch=1)
        self.enable_button = QPushButton("설정에서 켜기", self)
        self.enable_button.setProperty("variant", "secondary")
        self.enable_button.clicked.connect(self._on_open_settings)
        banner_row.addWidget(self.enable_button)
        layout.addLayout(banner_row)

        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(["순서", "사용", "이름", "계정", "동작", "응답 방식"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda _i: self._on_edit())
        layout.addWidget(self.table)

        row = QHBoxLayout()
        for text, slot, variant in (
            ("추가", self._on_add, "primary"),
            ("편집", self._on_edit, "secondary"),
            ("사용/중지", self._on_toggle_enabled, "secondary"),
            ("삭제", self._on_delete, "destructive"),
            ("▲ 위로", lambda: self._on_move(-1), "secondary"),
            ("▼ 아래로", lambda: self._on_move(1), "secondary"),
        ):
            button = QPushButton(text, self)
            button.setProperty("variant", variant)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        order_note = _plain_label(
            "위에 있는 규칙부터 차례로 검사해, 처음 맞는 규칙 하나만 적용합니다.", self
        )
        order_note.setProperty("role", "previewMeta")
        layout.addWidget(order_note)

        unmatched_row = QHBoxLayout()
        unmatched_row.addWidget(QLabel("어느 규칙에도 맞지 않는 메일:", self))
        self.unmatched_ignore = QRadioButton("무시(기본)", self)
        self.unmatched_draft = QRadioButton("초안 만들기", self)
        unmatched_row.addWidget(self.unmatched_ignore)
        unmatched_row.addWidget(self.unmatched_draft)
        unmatched_row.addStretch(1)
        layout.addLayout(unmatched_row)

        close_row = QHBoxLayout()
        help_button = QPushButton("도움말", self)
        help_button.setProperty("variant", "secondary")
        help_button.clicked.connect(self._on_help)
        close_row.addWidget(help_button)
        close_row.addStretch(1)
        close_button = QPushButton("닫기", self)
        close_button.setProperty("variant", "secondary")
        close_button.clicked.connect(self._on_close)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        self.refresh()

    # ------------------------------------------------------------ 읽기

    def refresh(self) -> None:
        api, bridge = self._api, self._bridge
        try:
            toggle = bridge.call_backend(api.get_autoreply_toggle(), timeout=5.0)
            self._autoreply_enabled = bool(isinstance(toggle, dict) and toggle.get("enabled"))
        except Exception:  # noqa: BLE001 — 읽지 못하면 꺼짐으로 표시(fail-closed)
            logger.warning("자동회신 상태 조회 실패", exc_info=True)
            self._autoreply_enabled = False
        self.banner.setVisible(not self._autoreply_enabled)
        self.enable_button.setVisible(not self._autoreply_enabled)
        try:
            self._rules = bridge.call_backend(api.list_rules(), timeout=5.0)
            accounts = bridge.call_backend(api.list_accounts(), timeout=5.0)
            unmatched = bridge.call_backend(api.get_unmatched_action(), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "규칙 설정", f"규칙을 불러오지 못했습니다: {exc}")
            return
        self._accounts = [(a.id, f"{a.display_name} <{a.email_address}>") for a in accounts]
        names = dict(self._accounts)
        self.table.setRowCount(len(self._rules))
        for r, rule in enumerate(self._rules):
            values = [
                str(r + 1),
                "사용" if rule["enabled"] else "중지",
                rule["name"],
                names.get(rule["account_id"], "모든 계정") if rule["account_id"] else "모든 계정",
                ACTION_LABELS.get(rule["action"], rule["action"]),
                REPLY_MODE_LABELS.get(rule["reply_mode"], rule["reply_mode"]),
            ]
            for c, value in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(value))
        (self.unmatched_draft if unmatched == "draft" else self.unmatched_ignore).setChecked(True)

    def _selected(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rules):
            return None
        return self._rules[row]

    # ------------------------------------------------------------ 동작

    def _open_editor(self, rule: dict | None) -> None:
        dialog = RuleEditDialog(
            api=self._api,
            bridge=self._bridge,
            rule=rule,
            accounts=getattr(self, "_accounts", []),
            autoreply_enabled=self._autoreply_enabled,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _on_add(self) -> None:
        self._open_editor(None)

    def _on_edit(self) -> None:
        rule = self._selected()
        if rule is None:
            QMessageBox.information(self, "규칙 편집", "편집할 규칙을 선택하세요.")
            return
        self._open_editor(rule)

    def _on_toggle_enabled(self) -> None:
        rule = self._selected()
        if rule is None:
            return
        try:
            self._bridge.call_backend(
                self._api.set_rule_enabled(rule["id"], not rule["enabled"]), timeout=5.0
            )
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "규칙 설정", f"바꾸지 못했습니다: {exc}")
        self.refresh()

    def _on_delete(self) -> None:
        rule = self._selected()
        if rule is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("규칙 삭제")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"규칙 '{rule['name']}'을(를) 지웁니다. 이 규칙으로 대기 중인 잡은 취소됩니다.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            self._bridge.call_backend(self._api.delete_rule(rule["id"]), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "규칙 삭제", f"지우지 못했습니다: {exc}")
        self.refresh()

    def _on_move(self, delta: int) -> None:
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < len(self._rules)):
            return
        ids = [r["id"] for r in self._rules]
        ids[row], ids[target] = ids[target], ids[row]
        try:
            self._bridge.call_backend(self._api.reorder_rules(ids), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "규칙 순서", f"순서를 바꾸지 못했습니다: {exc}")
        self.refresh()
        self.table.selectRow(target)

    def save_unmatched_action(self) -> None:
        value = "draft" if self.unmatched_draft.isChecked() else "ignore"
        try:
            self._bridge.call_backend(self._api.set_unmatched_action(value), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "규칙 설정", f"미매칭 처리를 저장하지 못했습니다: {exc}")

    def _on_close(self) -> None:
        self.save_unmatched_action()
        self.accept()

    def _on_open_settings(self) -> None:
        self.save_unmatched_action()
        if self._open_settings is not None:
            self._open_settings()
        self.refresh()

    def _on_help(self) -> None:
        viewer = ManualViewer(parent=self)
        viewer.show_section("04_규칙설정")
        viewer.exec()
