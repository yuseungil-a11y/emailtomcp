"""MainWindow — P0 골격.

메뉴/툴바/폴더트리/그리드/미리보기 자리만 있는 빈 레이아웃이다(DESIGN.md §11.1).
실제 데이터 연결(계정/메일 조회 등)은 P1에서 채운다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QSplitter,
    QStatusBar,
    QTableView,
    QTextBrowser,
    QToolBar,
    QTreeView,
    QWidget,
)

if TYPE_CHECKING:
    from emailtomcp.core.events import EventBus
    from emailtomcp.runtime.qt_bridge import QtBridge


class MainWindow(QMainWindow):
    """P0 골격 — 메뉴 | 툴바 | (폴더트리 | (그리드 / 미리보기)) | 상태줄."""

    def __init__(
        self,
        *,
        version: str,
        event_bus: EventBus | None = None,
        bridge: QtBridge | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._version = version
        self._event_bus = event_bus
        self._bridge = bridge

        self.setWindowTitle("EmailToMCP")
        self.resize(1200, 800)

        self._build_menu()
        self._build_toolbar()
        self._build_central_widget()
        self._build_status_bar()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        menu_bar.addMenu("파일(&F)")
        menu_bar.addMenu("편집(&E)")
        menu_bar.addMenu("메일(&M)")
        tools_menu = menu_bar.addMenu("도구(&T)")
        tools_menu.addAction("계정")
        tools_menu.addAction("규칙")
        tools_menu.addAction("설정")
        menu_bar.addMenu("Claude(&C)")
        menu_bar.addMenu("도움말(&H)")

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("메인 툴바", self)
        toolbar.setObjectName("mainToolBar")
        for label in ("받기", "새 메일", "회신", "전체회신", "전달", "삭제", "읽음토글"):
            toolbar.addAction(label)
        self.addToolBar(toolbar)

    def _build_central_widget(self) -> None:
        self._folder_tree = QTreeView(self)
        self._folder_tree.setObjectName("folderTree")
        self._folder_tree.setHeaderHidden(True)

        self._message_grid = QTableView(self)
        self._message_grid.setObjectName("messageGrid")

        self._preview = QTextBrowser(self)
        self._preview.setObjectName("messagePreview")
        self._preview.setOpenExternalLinks(False)
        self._preview.setPlaceholderText("메일을 선택하면 여기에 미리보기가 표시됩니다.")

        right_splitter = QSplitter(Qt.Orientation.Vertical, self)
        right_splitter.setObjectName("rightSplitter")
        right_splitter.addWidget(self._message_grid)
        right_splitter.addWidget(self._preview)
        right_splitter.setStretchFactor(0, 2)
        right_splitter.setStretchFactor(1, 1)

        main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        main_splitter.setObjectName("mainSplitter")
        main_splitter.addWidget(self._folder_tree)
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 3)

        self.setCentralWidget(main_splitter)

    def _build_status_bar(self) -> None:
        status_bar = QStatusBar(self)
        self._version_label = QLabel(f"EmailToMCP v{self._version}")
        self._version_label.setObjectName("versionLabel")
        status_bar.addPermanentWidget(self._version_label)
        self.setStatusBar(status_bar)
