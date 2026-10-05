"""작성 창 (DESIGN.md §11.2).

새 메일/회신/전체회신/전달(인라인·첨부) 네 가지 모드를 모두 이 창 하나로 처리한다.
창을 열 때 바로 초안(`drafts.status='draft'`)을 만들고, 이후 모든 수정은
`UiApi.update_compose()`로 그 초안을 덮어쓴다(30초 자동저장 + [보내기] 직전 저장).
사용자가 직접 쓰는 메일은 승인 절차가 없다 — [보내기]는 바로 Outbox로 전이한다
(§6.2 confirm은 MCP 경유 발송에만 적용, 이번 범위와 무관).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QCompleter,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from emailtomcp.ui import async_utils
from emailtomcp.ui.message_box import plain_information, plain_warning

logger = logging.getLogger(__name__)

_ADDR_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_MAX_ATTACHMENT_TOTAL_BYTES = 25 * 1024 * 1024
_AUTOSAVE_INTERVAL_MS = 30_000

# 출력 가드(rules/output_guard.py) finding 코드 → 작성 창 경고 배너 문구(§7.5 마지막 줄).
GUARD_FINDING_LABELS: dict[str, str] = {
    "url": "링크(URL)",
    "email": "이메일 주소",
    "phone": "전화번호",
    "account_number": "계좌번호로 보이는 숫자",
    "money": "금액",
    "promise": "확정·약속 표현",
    "quote_line": "인용 줄(>)",
    "original_copy": "받은 메일 원문을 그대로 옮긴 부분",
    "banned_word": "금칙어",
    "subject_url": "받은 메일 제목의 링크·주소",
    "empty_body": "빈 본문",
    "too_long": "최대 글자 수 초과",
    "guard_unknown": "점검 결과를 확인할 수 없음",
}


# [수정 후 발송]·잡 탭 [초안 열기]로 기존 초안을 열 때 맨 위 안내 문구(데카르트 33번 I-4).
# 자동회신 초안은 claude 실행 여부(G7 기록)로 구분한다 — 고정 템플릿 초안에 "Claude가 만든"이라고
# 쓰지 않는다. 키 None = 자동회신 초안이 아님(MCP [수정 후 발송] 초안).
EXISTING_DRAFT_LABELS: dict[str | None, str] = {
    None: "Claude가 만든 초안 — 내용을 확인·수정한 뒤 직접 보내세요.",
    "claude": "자동회신 초안(Claude 작성) — 내용을 확인·수정한 뒤 직접 보내세요.",
    "fixed_template": "자동회신 초안(고정 템플릿) — 내용을 확인·수정한 뒤 직접 보내세요.",
    "unknown": "자동회신 초안 — 내용을 확인·수정한 뒤 직접 보내세요.",
}


def guard_banner_text(findings: list[str], *, claude: bool = True) -> str:
    """가드 finding 목록 → 배너 문구(평문). 모르는 코드는 코드 그대로 보여 준다.

    `claude=False`(고정 템플릿 등 claude를 실행하지 않은 자동회신 초안)면 "Claude" 표기를 뺀다.
    """
    labels: list[str] = []
    for code in findings:
        label = GUARD_FINDING_LABELS.get(code, code)
        if label not in labels:
            labels.append(label)
    subject = "자동회신(Claude)이 만든 초안" if claude else "자동회신 초안"
    return (
        f"주의: {subject}에서 다음 항목이 발견되었습니다 — "
        + ", ".join(labels)
        + ". 받은 메일의 지시가 섞였을 수 있습니다. 링크·금액·계좌번호 등은 직접 확인한 뒤 "
        "보내세요. (초안이 처음 만들어졌을 때의 점검 결과입니다)"
    )


def _split_addrs(text: str) -> list[str]:
    return [a.strip() for a in re.split(r"[,;]", text) if a.strip()]


def _invalid_addrs(text: str) -> list[str]:
    """형식이 이상한 주소만 돌려준다(비어 있으면 전부 올바름)."""
    return [a for a in _split_addrs(text) if not _ADDR_RE.match(a)]


class ComposeWindow(QDialog):
    def __init__(
        self,
        *,
        account_id: int,
        draft_id: int,
        api: Any,
        bridge: Any,
        origin_label: str = "",
        discard_on_cancel: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._account_id = account_id
        self._draft_id = draft_id
        self._api = api
        self._bridge = bridge
        # 이미 있던 초안(예: MCP 승인 요청을 취소하고 [수정 후 발송])을 연 경우 [취소]가
        # 초안을 지우지 않는다 — 작성 창이 만든 초안만 지운다.
        self._discard_on_cancel = discard_on_cancel
        self._attachments: list[dict] = []
        self.setWindowTitle("메일 작성")
        self.resize(640, 560)
        self.setAcceptDrops(True)

        # 기존 초안을 연 경우 자동회신 초안의 생성 방식(claude/fixed_template/unknown). None이면
        # 자동회신 초안이 아니거나 아직 조회 전.
        self._autoreply_source: str | None = None
        layout = QVBoxLayout(self)
        self.origin_label_widget: QLabel | None = None
        if origin_label:
            self.origin_label_widget = QLabel(origin_label, self)
            layout.addWidget(self.origin_label_widget)
        # 출력 가드 경고 배너(§7.5) — 자동회신 초안을 열었고 finding이 있을 때만 보인다.
        # 내용에 메일 유래 문자열은 없지만 일관되게 평문으로만 표시한다.
        self.guard_banner = QLabel(self)
        self.guard_banner.setTextFormat(Qt.TextFormat.PlainText)
        self.guard_banner.setWordWrap(True)
        self.guard_banner.setProperty("badge", "external")
        self.guard_banner.setVisible(False)
        layout.addWidget(self.guard_banner)

        form = QFormLayout()
        self._to_edit = QLineEdit(self)
        form.addRow("받는사람", self._to_edit)
        self._cc_edit = QLineEdit(self)
        form.addRow("참조", self._cc_edit)
        self._bcc_edit = QLineEdit(self)
        form.addRow("숨은참조", self._bcc_edit)
        self._subject_edit = QLineEdit(self)
        form.addRow("제목", self._subject_edit)
        layout.addLayout(form)

        self._body_edit = QPlainTextEdit(self)
        layout.addWidget(self._body_edit, stretch=1)

        attach_row = QHBoxLayout()
        attach_row.addWidget(QLabel("첨부", self))
        add_attach_btn = QPushButton("파일 추가", self)
        add_attach_btn.clicked.connect(self._on_add_attachment_dialog)
        remove_attach_btn = QPushButton("선택 제거", self)
        remove_attach_btn.clicked.connect(self._on_remove_attachment)
        attach_row.addWidget(add_attach_btn)
        attach_row.addWidget(remove_attach_btn)
        attach_row.addStretch(1)
        layout.addLayout(attach_row)

        self._attachments_list = QListWidget(self)
        self._attachments_list.setMaximumHeight(100)
        layout.addWidget(self._attachments_list)

        button_row = QHBoxLayout()
        self._send_btn = QPushButton("보내기", self)
        self._send_btn.setProperty("variant", "primary")
        self._send_btn.clicked.connect(self._on_send)
        cancel_btn = QPushButton("취소", self)
        cancel_btn.setProperty("variant", "secondary")
        cancel_btn.clicked.connect(self._on_cancel)
        button_row.addStretch(1)
        button_row.addWidget(cancel_btn)
        button_row.addWidget(self._send_btn)
        layout.addLayout(button_row)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(_AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start()

        self._load_known_addresses()

    # --- 생성 진입점(클래스메서드) -------------------------------------------

    @classmethod
    def open_new(cls, account_id: int, *, api: Any, bridge: Any, parent: QWidget | None = None):
        draft_id = bridge.call_backend(
            api.new_draft(
                account_id, to_addrs=[], cc_addrs=[], bcc_addrs=[], subject="", body_text=""
            ),
            timeout=5.0,
        )
        return cls(account_id=account_id, draft_id=draft_id, api=api, bridge=bridge, parent=parent)

    @classmethod
    def open_reply(
        cls,
        account_id: int,
        source_message_id: int,
        *,
        reply_all: bool,
        api: Any,
        bridge: Any,
        parent: QWidget | None = None,
    ):
        draft_id = bridge.call_backend(
            api.reply_draft(account_id, source_message_id, reply_all), timeout=5.0
        )
        window = cls(
            account_id=account_id,
            draft_id=draft_id,
            api=api,
            bridge=bridge,
            origin_label=(
                "회신 초안 — 원본 첨부파일이 기본 포함됩니다(필요하면 선택 후 제거하세요)."
            ),
            parent=parent,
        )
        window._load_draft_fields()
        return window

    @classmethod
    def open_forward(
        cls,
        account_id: int,
        source_message_id: int,
        *,
        mode: str,
        api: Any,
        bridge: Any,
        parent: QWidget | None = None,
    ):
        draft_id = bridge.call_backend(
            api.forward_draft(account_id, source_message_id, mode), timeout=5.0
        )
        label = "전달(인라인) 초안" if mode == "inline" else "전달(첨부) 초안"
        window = cls(
            account_id=account_id,
            draft_id=draft_id,
            api=api,
            bridge=bridge,
            origin_label=label,
            parent=parent,
        )
        window._load_draft_fields()
        return window

    @classmethod
    def open_existing(
        cls,
        account_id: int,
        draft_id: int,
        *,
        api: Any,
        bridge: Any,
        parent: QWidget | None = None,
    ):
        """이미 있는 초안을 연다(§11.6 [수정 후 발송], 잡 탭 [초안 열기]).

        보내면 새 스냅샷으로 사용자가 직접 발송. 자동회신 초안이면 출력 가드 경고 배너를 띄운다.
        """
        window = cls(
            account_id=account_id,
            draft_id=draft_id,
            api=api,
            bridge=bridge,
            origin_label=EXISTING_DRAFT_LABELS[None],
            discard_on_cancel=False,
            parent=parent,
        )
        window._load_draft_fields()
        window._load_origin_label()
        window._load_guard_banner()
        return window

    @staticmethod
    def ask_forward_mode(parent: QWidget | None) -> str | None:
        box = QMessageBox(parent)
        box.setWindowTitle("전달 방식 선택")
        box.setText("어떤 방식으로 전달하시겠습니까?")
        inline_btn = box.addButton("인라인 전달", QMessageBox.ButtonRole.AcceptRole)
        attach_btn = box.addButton("첨부로 전달", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is inline_btn:
            return "inline"
        if clicked is attach_btn:
            return "attach"
        return None

    # --- 초안 필드 로드 ------------------------------------------------------

    def _load_draft_fields(self) -> None:
        try:
            draft = self._bridge.call_backend(self._api.get_draft(self._draft_id), timeout=5.0)
        except Exception:
            logger.warning("초안 로드 실패", exc_info=True)
            return
        if draft is None:
            return
        self._to_edit.setText(", ".join(draft.to_addrs))
        self._cc_edit.setText(", ".join(draft.cc_addrs))
        self._bcc_edit.setText(", ".join(draft.bcc_addrs))
        self._subject_edit.setText(draft.subject or "")
        self._body_edit.setPlainText(draft.body_text or "")
        self._attachments = list(draft.attachments)
        self._refresh_attachments_list()

    def _load_guard_banner(self) -> None:
        """자동회신 초안이면 그 잡의 출력 가드 결과를 조회해 경고 배너로 보여 준다(§7.5)."""
        getter = getattr(self._api, "get_draft_guard_findings", None)
        if getter is None:
            return
        try:
            findings = self._bridge.call_backend(getter(self._draft_id), timeout=5.0)
        except Exception:
            # 자동회신 초안인지조차 모르므로 배너를 띄우지 않는다(로컬 DB 읽기라 사실상 없음).
            logger.warning("출력 가드 결과 조회 실패", exc_info=True)
            return
        if not findings:
            self.guard_banner.setVisible(False)
            return
        claude = self._autoreply_source not in ("fixed_template", "unknown")
        self.guard_banner.setText(guard_banner_text([str(f) for f in findings], claude=claude))
        self.guard_banner.setVisible(True)

    def _load_origin_label(self) -> None:
        """자동회신 초안이면 claude 실행 여부에 맞춰 맨 위 안내 문구를 바꾼다(데카르트 33번 I-4)."""
        getter = getattr(self._api, "get_autoreply_draft_source", None)
        if getter is None or self.origin_label_widget is None:
            return
        try:
            source = self._bridge.call_backend(getter(self._draft_id), timeout=5.0)
        except Exception:
            logger.warning("자동회신 초안 생성 방식 조회 실패", exc_info=True)
            return
        if source not in EXISTING_DRAFT_LABELS:
            source = "unknown"
        self._autoreply_source = source
        self.origin_label_widget.setText(EXISTING_DRAFT_LABELS[source])

    def _load_known_addresses(self) -> None:
        try:
            addrs = self._bridge.call_backend(
                self._api.list_known_addresses(self._account_id), timeout=5.0
            )
        except Exception:
            addrs = []
        completer = QCompleter(addrs, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        for edit in (self._to_edit, self._cc_edit, self._bcc_edit):
            edit.setCompleter(completer)

    # --- 첨부 -----------------------------------------------------------------

    def _refresh_attachments_list(self) -> None:
        self._attachments_list.clear()
        for meta in self._attachments:
            name = meta.get("filename") or "(이름 없음)"
            size = meta.get("size_bytes")
            label = f"{name} ({size // 1024}KB)" if size else str(name)
            self._attachments_list.addItem(QListWidgetItem(label))

    def _total_attachment_bytes(self) -> int:
        return sum(int(m.get("size_bytes") or 0) for m in self._attachments)

    def _on_add_attachment_dialog(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(self, "첨부 파일 선택")
        for path in paths:
            self._add_local_attachment(path)

    def _add_local_attachment(self, path: str) -> None:
        try:
            meta = self._bridge.call_backend(self._api.stage_local_attachment(path), timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            plain_warning(self, "첨부", f"첨부하지 못했습니다: {exc}")
            return
        self._attachments.append(meta)
        self._refresh_attachments_list()
        if self._total_attachment_bytes() > _MAX_ATTACHMENT_TOTAL_BYTES:
            QMessageBox.warning(self, "첨부 용량", "첨부 총 용량이 25MB를 넘었습니다.")
        self._autosave()

    def _on_remove_attachment(self) -> None:
        row = self._attachments_list.currentRow()
        if row < 0 or row >= len(self._attachments):
            return
        del self._attachments[row]
        self._refresh_attachments_list()
        self._autosave()

    # --- 드래그앤드롭 -----------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 — Qt 오버라이드
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 — Qt 오버라이드
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                self._add_local_attachment(path)
        event.acceptProposedAction()

    # --- 저장/발송 --------------------------------------------------------------

    def _collect_fields(self) -> dict:
        return {
            "to_addrs": _split_addrs(self._to_edit.text()),
            "cc_addrs": _split_addrs(self._cc_edit.text()),
            "bcc_addrs": _split_addrs(self._bcc_edit.text()),
            "subject": self._subject_edit.text().strip() or None,
            "body_text": self._body_edit.toPlainText(),
            "attachments": self._attachments,
        }

    def _autosave(self) -> bool:
        """화면 내용을 초안에 저장한다. 실패하면 경고 로그를 남기고 False.

        [보내기]는 이 함수를 쓰지 않는다 — 저장과 발송을 백엔드 한 트랜잭션으로 처리하는
        `send_draft_now(draft_id, **fields)`를 쓴다(보안검토 N-1/R-1).
        """
        fields = self._collect_fields()
        try:
            self._bridge.call_backend(
                self._api.update_compose(self._draft_id, **fields), timeout=5.0
            )
        except Exception:
            logger.warning("작성 중 자동저장 실패", exc_info=True)
            return False
        return True

    def _validate_before_send(self) -> str | None:
        fields = self._collect_fields()
        if not fields["to_addrs"]:
            return "받는사람을 입력하세요."
        bad: list[str] = []
        for text in (self._to_edit.text(), self._cc_edit.text(), self._bcc_edit.text()):
            bad.extend(_invalid_addrs(text))
        if bad:
            return f"주소 형식이 올바르지 않습니다: {', '.join(bad)}"
        return None

    def _on_send(self) -> None:
        error = self._validate_before_send()
        if error:
            # 오류 문구에 메일에서 온 주소(회신 Reply-To 등)가 그대로 들어간다 — 평문으로만
            # 표시한다(보안검토 N-2).
            plain_warning(self, "보내기", error)
            return
        # 화면에 보이는 내용을 그대로 넘긴다. 백엔드가 저장과 Outbox 전이를 writer 작업
        # 하나로 처리하므로, 그 사이에 MCP update_draft가 끼어들 틈이 없다(보안검토 N-1/R-1).
        # 저장이 실패하면 발송도 일어나지 않고 아래 _done에서 오류로 알린다.
        fields = self._collect_fields()
        self._send_btn.setEnabled(False)
        self._send_btn.setText("발송 중...")

        def _done(future) -> None:  # noqa: ANN001
            self._send_btn.setEnabled(True)
            self._send_btn.setText("보내기")
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                plain_warning(self, "보내기", f"발송 요청에 실패했습니다: {exc}")
                return
            if result == "sent":
                self.accept()
            elif result == "retry_or_failed":
                plain_information(self, "보내기", "일시적인 오류로 재시도 대기열로 보냈습니다.")
                self.accept()
            else:
                plain_warning(self, "보내기", f"발송 결과: {result}")

        async_utils.run_async(
            self._bridge, self._api.send_draft_now(self._draft_id, **fields), _done, parent=self
        )

    def _on_cancel(self) -> None:
        self._autosave_timer.stop()
        if not self._discard_on_cancel:
            self._autosave()
            self.reject()
            return
        try:
            self._bridge.call_backend(self._api.discard_draft(self._draft_id), timeout=5.0)
        except Exception:
            logger.warning(
                "초안 취소 중 삭제 실패(이미 발송 이력이 있는 초안일 수 있음)", exc_info=True
            )
        self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt 오버라이드
        self._autosave_timer.stop()
        self._autosave()
        super().closeEvent(event)
