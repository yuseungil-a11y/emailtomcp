"""폴더 트리 모델 (DESIGN.md §11.1).

`QStandardItemModel`을 확장해 계정/폴더/가상폴더("작업" 그룹) 노드에 도메인
데이터(계정 ID, 폴더 ID, 가상폴더 종류, 연결상태 점)를 커스텀 Role로 얹는다.
완전히 새로운 `QAbstractItemModel`을 처음부터 구현하는 대신 이 방식을 택했다 —
항목 수가 적고(계정 수 × 폴더 수) 갱신(배지·상태 점)이 빈번한 트리 특성상
`QStandardItemModel` 기반이 구현량 대비 충분하다(완료 보고에 기록한 단순화).
"""

from __future__ import annotations

import html

from PySide6.QtCore import QModelIndex, QObject, Qt
from PySide6.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel

ROLE_ACCOUNT_ID = Qt.ItemDataRole.UserRole + 1
ROLE_FOLDER_ID = Qt.ItemDataRole.UserRole + 2
ROLE_VIRTUAL_KIND = Qt.ItemDataRole.UserRole + 3  # "outbox" | "pending_approval"

# `docs/design/UI_디자인가이드.md` §3, light.qss/dark.qss의 `statusDot` 속성값과 맞춘다.
STATUS_COLORS: dict[str, QColor] = {
    "connected": QColor("#248ff4"),
    "connecting": QColor("#8a90fc"),
    "disconnected": QColor("#f15347"),
}
STATUS_LABELS: dict[str, str] = {
    "connected": "정상",
    "connecting": "연결중",
    "disconnected": "오류",
}


class FolderTreeModel(QStandardItemModel):
    """열(0)=이름/배지, 열(1)=계정 연결상태 점(계정 행에만 채운다)."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["폴더", ""])
        self._account_items: dict[int, QStandardItem] = {}
        self._folder_items: dict[int, QStandardItem] = {}
        self._virtual_items: dict[str, QStandardItem] = {}

    def clear_tree(self) -> None:
        self.removeRows(0, self.rowCount())
        self._account_items.clear()
        self._folder_items.clear()
        self._virtual_items.clear()

    @staticmethod
    def _label(base: str, count: int) -> str:
        return f"{base}({count})" if count else base

    def add_account(self, account_id: int, display_name: str) -> QStandardItem:
        name_item = QStandardItem(display_name)
        name_item.setEditable(False)
        name_item.setData(account_id, ROLE_ACCOUNT_ID)
        status_item = QStandardItem("")
        status_item.setEditable(False)
        self.invisibleRootItem().appendRow([name_item, status_item])
        self._account_items[account_id] = name_item
        self.set_account_status(account_id, "connecting")
        return name_item

    def add_folder(
        self, account_item: QStandardItem, folder_id: int, label: str, *, unread: int = 0
    ) -> QStandardItem:
        item = QStandardItem(self._label(label, unread))
        item.setEditable(False)
        item.setData(folder_id, ROLE_FOLDER_ID)
        account_item.appendRow([item, QStandardItem("")])
        self._folder_items[folder_id] = item
        return item

    def add_group(self, label: str) -> QStandardItem:
        item = QStandardItem(label)
        item.setEditable(False)
        self.invisibleRootItem().appendRow([item, QStandardItem("")])
        return item

    def add_virtual_folder(
        self, group_item: QStandardItem, kind: str, label: str, *, count: int = 0
    ) -> QStandardItem:
        item = QStandardItem(self._label(label, count))
        item.setEditable(False)
        item.setData(kind, ROLE_VIRTUAL_KIND)
        group_item.appendRow([item, QStandardItem("")])
        self._virtual_items[kind] = item
        return item

    def set_folder_unread(self, folder_id: int, label: str, unread: int) -> None:
        item = self._folder_items.get(folder_id)
        if item is not None:
            item.setText(self._label(label, unread))

    def virtual_index(self, kind: str) -> QModelIndex:
        """가상 폴더("outbox" | "pending_approval") 행의 인덱스. 없으면 무효 인덱스."""
        item = self._virtual_items.get(kind)
        return item.index() if item is not None else QModelIndex()

    def set_virtual_count(self, kind: str, label: str, count: int) -> None:
        item = self._virtual_items.get(kind)
        if item is not None:
            item.setText(self._label(label, count))

    def set_account_status(self, account_id: int, status: str, tooltip: str = "") -> None:
        name_item = self._account_items.get(account_id)
        if name_item is None:
            return
        parent = name_item.parent() or self.invisibleRootItem()
        status_item = parent.child(name_item.row(), 1)
        if status_item is None:
            return
        color = STATUS_COLORS.get(status, STATUS_COLORS["connecting"])
        status_item.setText("●" + STATUS_LABELS.get(status, ""))
        status_item.setForeground(QBrush(color))
        # IMAP 서버가 돌려준 오류 문자열이 그대로 들어올 수 있다 — 툴팁은 Qt가 AutoText로
        # 해석해 리치텍스트(원격/UNC 이미지 로드 등)로 보일 수 있으므로 escape한다(N-5).
        status_item.setToolTip(html.escape(tooltip))

    def folder_id_at(self, index: QModelIndex) -> int | None:
        item = self.itemFromIndex(index)
        return item.data(ROLE_FOLDER_ID) if item else None

    def virtual_kind_at(self, index: QModelIndex) -> str | None:
        item = self.itemFromIndex(index)
        return item.data(ROLE_VIRTUAL_KIND) if item else None

    def account_id_at(self, index: QModelIndex) -> int | None:
        item = self.itemFromIndex(index)
        return item.data(ROLE_ACCOUNT_ID) if item else None
