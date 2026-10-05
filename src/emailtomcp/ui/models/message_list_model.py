"""메일 그리드 모델 (DESIGN.md §11.1, §1.2 #27 "1만 건 폴더 2초" 대량 성능 기준).

`QAbstractTableModel` + `fetchMore()`로 200건씩 페이지 단위 로딩한다. 모델 스스로는
어떤 I/O도 하지 않는다 — `fetchMore()`는 생성자에서 주입받은 콜백(MainWindow가
`qt_bridge.call_backend()`로 실제 조회를 수행)을 호출할 뿐이다. 정렬도 같은 이유로
`sort()`를 직접 구현하지 않고 `sortRequested` Signal만 내보낸다(부분 로딩된 데이터를
메모리에서만 정렬하면 전체 순서가 틀어지므로, 매번 DB에 새로 질의하는 쪽이 맞다).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont

if TYPE_CHECKING:
    from emailtomcp.storage.repositories.messages import MessageRow

PAGE_SIZE = 200

# B008 회피용 모듈 전역 싱글턴(루트 인덱스) — 매 호출마다 `QModelIndex()`를 새로 만들지
# 않고 이 값을 기본 인자로 재사용한다(ruff 권고).
_ROOT_INDEX = QModelIndex()

COLUMNS: tuple[str, ...] = (
    "unread",
    "flag",
    "attach",
    "priority",
    "from",
    "subject",
    "received",
    "size",
    "auto",
)
# "자동회신" 열 표시(§5.2 messages.auto_reply_status, P3 Phase B). NULL(평가 대상 아님)은 빈 칸.
AUTO_REPLY_STATUS_LABELS: dict[str, str] = {
    "ignored": "무시",
    "blocked": "차단",
    "queued": "대기",
    "running": "작성 중",
    "drafted": "초안",
    "skipped": "건너뜀",
    "failed": "실패",
    "cancelled": "취소",
    "sent": "발송",
}

HEADERS: tuple[str, ...] = ("", "★", "📎", "!", "보낸사람", "제목", "받은시간", "크기", "자동회신")

# 헤더 클릭 정렬 시 UiApi.list_messages의 sort_column 인자로 넘길 키(없으면 "received_at" 폴백).
SORT_KEYS: dict[str, str] = {
    "from": "from",
    "subject": "subject",
    "received": "received_at",
    "size": "size",
    "priority": "priority",
}


def format_size(size: int | None) -> str:
    if not size:
        return ""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


_Index = QModelIndex | QPersistentModelIndex


class MessageListModel(QAbstractTableModel):
    sortRequested = Signal(str, bool)  # (sort_column, descending)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[MessageRow] = []
        self._total = 0
        self._fetch_more_cb: Callable[[int], None] | None = None

    # --- 외부 제어 ---------------------------------------------------------

    def set_fetch_more_callback(self, callback: Callable[[int], None] | None) -> None:
        self._fetch_more_cb = callback

    def reset_rows(self, rows: list[MessageRow], total: int) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._total = total
        self.endResetModel()

    def append_rows(self, rows: list[MessageRow]) -> None:
        if not rows:
            return
        start = len(self._rows)
        self.beginInsertRows(QModelIndex(), start, start + len(rows) - 1)
        self._rows.extend(rows)
        self.endInsertRows()

    def row_at(self, row: int) -> MessageRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def row_count_loaded(self) -> int:
        return len(self._rows)

    # --- QAbstractTableModel ----------------------------------------------

    def rowCount(self, parent: _Index = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = _ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def canFetchMore(self, parent: _Index = _ROOT_INDEX) -> bool:
        if parent.isValid():
            return False
        return len(self._rows) < self._total

    def fetchMore(self, parent: _Index = _ROOT_INDEX) -> None:
        if parent.isValid() or self._fetch_more_cb is None:
            return
        self._fetch_more_cb(len(self._rows))

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = COLUMNS[index.column()]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(row, col)
        if role == Qt.ItemDataRole.FontRole and col == "subject":
            font = QFont()
            font.setBold(not row.is_read)
            return font
        if role == Qt.ItemDataRole.ForegroundRole and col in ("from", "subject"):
            return QColor("#2c2d2e") if not row.is_read else QColor("#717174")
        if role == Qt.ItemDataRole.ToolTipRole and col == "subject" and row.parse_limited:
            return "파싱 제한 모드로 저장된 메일입니다(원본 용량/구조 제한 초과)."
        return None

    @staticmethod
    def _display_value(row: MessageRow, col: str) -> str:
        if col == "unread":
            return "" if row.is_read else "●"
        if col == "flag":
            return "★" if row.is_flagged else ""
        if col == "attach":
            return "📎" if row.has_attachments else ""
        if col == "priority":
            return "!" if row.priority is not None and row.priority <= 2 else ""
        if col == "from":
            return row.from_name or row.from_addr or ""
        if col == "subject":
            subject = row.subject or "(제목 없음)"
            return f"[파싱제한] {subject}" if row.parse_limited else subject
        if col == "received":
            return row.received_at[:16].replace("T", " ")
        if col == "size":
            return format_size(row.size_bytes)
        if col == "auto":
            return AUTO_REPLY_STATUS_LABELS.get(row.auto_reply_status or "", "")
        return ""

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        col_name = COLUMNS[column] if 0 <= column < len(COLUMNS) else "received"
        sort_key = SORT_KEYS.get(col_name, "received_at")
        self.sortRequested.emit(sort_key, order == Qt.SortOrder.DescendingOrder)
