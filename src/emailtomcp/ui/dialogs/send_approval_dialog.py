"""발송 승인 다이얼로그 (DESIGN.md §11.6, H9, M5).

Claude(MCP)가 `send_draft`를 호출하면 백엔드가 `ApprovalRequested`를 발행하고, 메인 창이
이 다이얼로그를 띄운다.

- 수신자 전체(받는사람/참조/숨은참조), 제목, 본문, 첨부를 모두 보여 준다. 계정 도메인과
  다른 도메인의 수신자는 `favorite` 색 "외부" 배지로 강조한다.
- **포커스를 빼앗지 않는다**(`WA_ShowWithoutActivating`). 메인 창이 OS 알림과 작업표시줄
  깜빡임으로 알리고, 사용자가 클릭해서 이 창으로 온다.
- 기본 버튼은 **[거부]**다. 창이 뜬 뒤 1.5초 동안은 모든 입력을 받지 않고(버튼 비활성 +
  키 입력 무시), 그 뒤에 버튼을 활성화한다 — 다른 창에서 타이핑하던 Enter로 승인되는
  사고를 막는다(M5).
- [승인]은 다이얼로그가 보여 준 내용의 스냅샷 해시를 함께 보낸다. 백엔드는 이 해시가
  승인 요청 시점의 해시·현재 초안 해시와 모두 같을 때만 발송한다(H9).
- [수정 후 발송]은 승인 요청을 취소하고 작성 창을 연다(사용자가 직접 보내면 새 스냅샷).
- 120초 카운트다운이 끝나도 창은 닫지 않는다. 초안은 "승인 대기" 함에 24시간 남는다.
- 창을 닫거나 Esc를 누르면 결정 없이 닫힌다(초안은 승인 대기 상태 그대로).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from emailtomcp.ui.message_box import plain_warning

logger = logging.getLogger(__name__)

INPUT_LOCK_MS = 1500
COUNTDOWN_SECONDS = 120


def _plain_label(text: str, parent: QWidget) -> QLabel:
    """비신뢰 문자열(제목·주소·계정 표시명 등)을 **평문으로만** 보여 주는 QLabel.

    QLabel 기본값(`Qt.AutoText`)은 `<img src="file://공격자/share/a.png">` 같은 문자열을
    리치텍스트로 해석한다. 이 다이얼로그는 사용자가 클릭하지 않아도 자동으로 뜨므로,
    그대로 두면 창이 뜨는 순간 UNC 경로 로드(NetNTLM 해시 유출)나 승인 화면 위장이 가능하다
    (보안검토 H-3). 그래서 이 창의 QLabel은 전부 이 함수로 만든다.
    """
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    return label


def _recipient_row(parent: QWidget, addrs: list[str], external: set[str]) -> QWidget:
    """주소 목록 한 줄. 외부 도메인 주소 옆에 "외부" 배지를 붙인다."""
    row = QWidget(parent)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    if not addrs:
        layout.addWidget(_plain_label("(없음)", row))
    for addr in addrs:
        label = _plain_label(addr, row)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        if addr in external:
            badge = _plain_label("외부", row)
            badge.setProperty("badge", "external")
            badge.setToolTip("내 계정과 다른 도메인의 수신자입니다.")
            layout.addWidget(badge)
    layout.addStretch(1)
    return row


class SendApprovalDialog(QDialog):
    """MCP 발송 승인 창 하나(초안 1건)."""

    def __init__(
        self,
        *,
        draft_id: int,
        api: Any,
        bridge: Any,
        on_edit_requested: Callable[[int, int], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._draft_id = draft_id
        self._api = api
        self._bridge = bridge
        self._on_edit_requested = on_edit_requested
        self._input_locked = True
        self._lock_started = False
        self._remaining = COUNTDOWN_SECONDS
        self._view: Any = None

        self.setObjectName("sendApprovalDialog")
        self.setWindowTitle("Claude 발송 승인 요청")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(620, 600)

        layout = QVBoxLayout(self)
        header = _plain_label(
            "Claude가 아래 메일의 발송을 요청했습니다. 내용을 확인한 뒤 결정하세요.", self
        )
        header.setProperty("role", "previewSubject")
        header.setWordWrap(True)
        layout.addWidget(header)

        self._form = QFormLayout()
        layout.addLayout(self._form)

        self._body = QPlainTextEdit(self)
        self._body.setReadOnly(True)
        layout.addWidget(self._body, stretch=1)

        layout.addWidget(_plain_label("첨부", self))
        self._attachments = QListWidget(self)
        self._attachments.setMaximumHeight(90)
        layout.addWidget(self._attachments)

        self._countdown_label = _plain_label("", self)
        self._countdown_label.setProperty("role", "previewMeta")
        layout.addWidget(self._countdown_label)

        buttons = QHBoxLayout()
        self.reject_button = QPushButton("거부", self)
        self.reject_button.setProperty("variant", "secondary")
        self.reject_button.setDefault(True)
        self.reject_button.setAutoDefault(True)
        self.reject_button.clicked.connect(self._on_reject_clicked)
        self.edit_button = QPushButton("수정 후 발송", self)
        self.edit_button.setProperty("variant", "secondary")
        self.edit_button.setAutoDefault(False)
        self.edit_button.clicked.connect(self._on_edit_clicked)
        self.approve_button = QPushButton("승인", self)
        self.approve_button.setProperty("variant", "primary")
        self.approve_button.setAutoDefault(False)
        self.approve_button.clicked.connect(self._on_approve_clicked)
        buttons.addStretch(1)
        buttons.addWidget(self.reject_button)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.approve_button)
        layout.addLayout(buttons)

        self._set_buttons_enabled(False)

        self._unlock_timer = QTimer(self)
        self._unlock_timer.setSingleShot(True)
        self._unlock_timer.setInterval(INPUT_LOCK_MS)
        self._unlock_timer.timeout.connect(self._unlock_inputs)

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick)

        self._load()

    # ------------------------------------------------------------------ 상태

    @property
    def draft_id(self) -> int:
        return self._draft_id

    @property
    def is_input_locked(self) -> bool:
        return self._input_locked

    def _set_buttons_enabled(self, enabled: bool) -> None:
        loaded = self._view is not None
        for button in (self.reject_button, self.edit_button, self.approve_button):
            button.setEnabled(enabled and loaded)

    def _add_row(self, name: str, field: QWidget) -> None:
        self._form.addRow(_plain_label(name, self), field)

    def _load(self) -> None:
        try:
            view = self._bridge.call_backend(
                self._api.get_approval_view(self._draft_id), timeout=5.0
            )
        except Exception:  # noqa: BLE001
            logger.warning("승인 대상 초안 로드 실패: draft_id=%s", self._draft_id, exc_info=True)
            view = None
        self._view = view
        if view is None:
            self._form.addRow(_plain_label("이 초안은 더 이상 승인 대기 상태가 아닙니다.", self))
            return
        draft = view.draft
        external = set(view.external_recipients)
        # 행 이름도 _plain_label로 넘긴다 — 이 창의 QLabel이 전부 평문인지 테스트로 단언한다.
        self._add_row("보내는 계정", _plain_label(view.account_label, self))
        self._add_row("받는사람", _recipient_row(self, draft.to_addrs, external))
        self._add_row("참조", _recipient_row(self, draft.cc_addrs, external))
        self._add_row("숨은참조", _recipient_row(self, draft.bcc_addrs, external))
        subject = _plain_label(draft.subject or "(제목 없음)", self)
        subject.setWordWrap(True)
        self._add_row("제목", subject)
        if external:
            warn = _plain_label(f"외부 도메인 수신자 {len(external)}명이 포함되어 있습니다.", self)
            warn.setProperty("badge", "external")
            self._add_row("", warn)
        self._body.setPlainText(draft.body_text or "")
        for meta in draft.attachments:
            name = meta.get("filename") or "(이름 없음)"
            size = meta.get("size_bytes")
            self._attachments.addItem(f"{name} ({int(size) // 1024}KB)" if size else str(name))
        if not draft.attachments:
            self._attachments.addItem("(첨부 없음)")
        self._update_countdown_label()

    # ------------------------------------------------------------------ 입력 지연(M5)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 — Qt 오버라이드
        super().showEvent(event)
        if not self._lock_started:
            self._lock_started = True
            self._unlock_timer.start()
            self._countdown_timer.start()

    def _unlock_inputs(self) -> None:
        self._input_locked = False
        self._set_buttons_enabled(True)
        self.reject_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 — Qt 오버라이드
        if self._input_locked:
            event.ignore()
            return
        super().keyPressEvent(event)

    def _tick(self) -> None:
        self._remaining = max(0, self._remaining - 1)
        self._update_countdown_label()
        if self._remaining == 0:
            self._countdown_timer.stop()

    def _update_countdown_label(self) -> None:
        if self._remaining > 0:
            self._countdown_label.setText(
                f"{self._remaining}초 안에 결정하지 않으면 Claude에는 '승인 대기 중'으로 알리고, "
                "이 메일은 '승인 대기' 함에 남습니다(24시간)."
            )
        else:
            self._countdown_label.setText(
                "응답 시간이 지나 '승인 대기' 함에 남았습니다. 지금 승인하거나 거부해도 됩니다."
            )

    # ------------------------------------------------------------------ 결정

    def _on_approve_clicked(self) -> None:
        if self._input_locked or self._view is None:
            return
        try:
            outcome = self._bridge.call_backend(
                self._api.approve_draft(self._draft_id, self._view.snapshot_hash), timeout=10.0
            )
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "승인", f"승인 처리에 실패했습니다: {exc}")
            return
        if outcome.ok:
            self.accept()
        else:
            plain_warning(self, "승인", outcome.message)
            if outcome.decision is not None:
                self.done(QDialog.DialogCode.Rejected)

    def _on_reject_clicked(self) -> None:
        if self._input_locked:
            return
        try:
            self._bridge.call_backend(self._api.reject_draft(self._draft_id), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "거부", f"거부 처리에 실패했습니다: {exc}")
            return
        self.done(QDialog.DialogCode.Rejected)

    def _on_edit_clicked(self) -> None:
        if self._input_locked or self._view is None:
            return
        try:
            ok = self._bridge.call_backend(
                self._api.cancel_approval_for_edit(self._draft_id), timeout=5.0
            )
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "수정 후 발송", f"처리에 실패했습니다: {exc}")
            return
        if ok and self._on_edit_requested is not None:
            self._on_edit_requested(self._view.draft.account_id, self._draft_id)
        self.done(QDialog.DialogCode.Rejected)

    def handle_resolved_elsewhere(self) -> None:
        """다른 경로(만료, 다른 창)로 결정이 났을 때 메인 창이 호출한다."""
        self._countdown_timer.stop()
        self.done(QDialog.DialogCode.Rejected)
