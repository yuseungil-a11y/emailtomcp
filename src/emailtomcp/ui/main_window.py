"""MainWindow — 메인 화면 (DESIGN.md §11.1).

메뉴 | 툴바 | (폴더트리 | (그리드 / 미리보기)) | 상태줄. 백엔드 호출은 전부
`qt_bridge.call_backend()`(짧은 로컬 DB 호출, 동기 대기) 또는
`qt_bridge.call_backend_async()` + `ui.async_utils.run_async`(네트워크 I/O가 섞인
호출, Qt 메인 스레드를 막지 않음) 경로로만 한다 — `ui` 패키지는 storage/mail을
직접 import하지 않는다(§4.2 의존 규칙, `emailtomcp.app.UiApi`가 유일한 창구).
"""

from __future__ import annotations

import json
import logging
import tempfile
from importlib import resources
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QModelIndex, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QTableView,
    QTextBrowser,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from emailtomcp.ui import async_utils
from emailtomcp.ui.dialogs.account_dialog import AccountManagerDialog
from emailtomcp.ui.dialogs.compose_window import ComposeWindow
from emailtomcp.ui.dialogs.manual_viewer import ManualViewer
from emailtomcp.ui.dialogs.mcp_panel import McpPanelDialog
from emailtomcp.ui.dialogs.raw_source_viewer import RawSourceViewer
from emailtomcp.ui.dialogs.rule_editor_dialog import RuleManagerDialog
from emailtomcp.ui.dialogs.send_approval_dialog import SendApprovalDialog
from emailtomcp.ui.dialogs.settings_dialog import TAB_AUTOREPLY, SettingsDialog
from emailtomcp.ui.message_box import plain_information, plain_warning
from emailtomcp.ui.models.folder_tree_model import FolderTreeModel
from emailtomcp.ui.models.message_list_model import PAGE_SIZE, MessageListModel
from emailtomcp.ui.widgets.update_banner import UpdateBanner, describe_update_status, status_badge

if TYPE_CHECKING:
    from emailtomcp.app import UiApi
    from emailtomcp.core.events import EventBus
    from emailtomcp.runtime.qt_bridge import QtBridge

logger = logging.getLogger(__name__)

ICON_PACKAGE = "emailtomcp.ui.resources.icons"

_ROLE_LABELS: dict[str, str] = {
    "inbox": "받은편지함",
    "sent": "보낸편지함",
    "drafts": "임시보관함",
    "trash": "휴지통",
    "junk": "스팸메일함",
    "archive": "보관함",
}

_FILTER_LABELS = ("전체", "안읽음", "플래그", "첨부있음")

_OUTBOX_STATUS_LABELS: dict[str, str] = {
    "outbox": "대기",
    "sending": "발송중",
    "failed": "실패",
    "send_unknown": "결과불명",
}


KILL_SWITCH_TOOLTIP_OFF = "자동회신이 꺼져 있습니다"
KILL_SWITCH_TOOLTIP_PENDING = "긴급정지 기능은 이후 단계에서 제공됩니다"


class _ClickableLabel(QLabel):
    """클릭할 수 있는 상태줄 라벨(자동회신 표시 → 설정 > 자동회신 탭, §11.1)."""

    clicked = Signal()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt 오버라이드
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _folder_label(role: str | None, name: str) -> str:
    return _ROLE_LABELS.get(role or "", name)


def _safe_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _load_icon() -> QIcon:
    try:
        icon_path = resources.files(ICON_PACKAGE).joinpath("app_icon_256.png")
        with resources.as_file(icon_path) as path:
            return QIcon(str(path))
    except (FileNotFoundError, ModuleNotFoundError):
        return QIcon()


class SecureMailBrowser(QTextBrowser):
    """미리보기 본문(§8.4 H5 하드닝).

    본문(`messages.body_text`)은 수신 시점에 이미 평문으로 변환돼 있어(HTML이면
    `html2text`로 변환, `mail/mime/parse.py` 참조) 화면에 원본 HTML을 그대로 그릴
    일이 없다 — 즉 지금은 `cid:` 참조나 외부 리소스 로딩 자체가 생기지 않는다.
    그래도 향후 HTML 표시를 추가하더라도 안전하도록 `loadResource`를 방어적으로
    완전히 차단하고, 링크 클릭은 http/https/mailto만 확인 다이얼로그를 거쳐 시스템
    브라우저로 연다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._on_anchor_clicked)

    def loadResource(self, type_: int, name: object) -> object:  # noqa: N802 — Qt 오버라이드 시그니처
        return None

    def _on_anchor_clicked(self, url) -> None:  # noqa: ANN001 — QUrl
        scheme = url.scheme().lower()
        if scheme not in ("http", "https", "mailto"):
            return
        reply = QMessageBox.question(
            self,
            "링크 열기",
            f"이 링크를 시스템 브라우저로 여시겠습니까?\n\n{url.toString()}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(url)


class MainWindow(QMainWindow):
    """메인 창 — 폴더 트리 | 메일 그리드 / 미리보기 | 상태줄."""

    def __init__(
        self,
        *,
        version: str,
        event_bus: EventBus | None = None,
        bridge: QtBridge | None = None,
        ui_api: UiApi | None = None,
        apply_theme=None,  # noqa: ANN001 — Callable[[str], None], app.py가 주입
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._version = version
        self._event_bus = event_bus
        self._bridge = bridge
        self._api = ui_api
        self._apply_theme = apply_theme or (lambda _pref: None)

        self._account_status: dict[int, tuple[str, str]] = {}
        self._folder_account_map: dict[int, int] = {}
        self._current_account_id: int | None = None
        self._current_folder_id: int | None = None
        self._current_virtual_kind: str | None = None
        self._filter_unread = False
        self._filter_flagged = False
        self._filter_attachments = False
        self._search_text = ""
        self._sort_column = "received_at"
        self._sort_descending = True
        # MCP 발송 승인 창(§11.6) — 초안 ID별로 하나만 띄운다.
        self._approval_dialogs: dict[int, SendApprovalDialog] = {}
        # 마지막 트레이 알림이 승인 요청이었다면 그 초안 ID(알림 클릭 시 승인 창을 연다).
        self._tray_approval_draft_id: int | None = None

        self.setWindowTitle("EmailToMCP")
        self.resize(1200, 800)
        self.setWindowIcon(_load_icon())

        self._qsettings = QSettings("UTInfo", "EmailToMCP")

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._build_central_widget()
        self._build_status_bar()
        self._build_tray_icon()
        self._restore_column_widths()

        if self._bridge is not None:
            self._bridge.event_received.connect(self._on_backend_event)

        if self._api is not None:
            self._reload_tree()
            self._apply_preview_position()
            self._refresh_update_status()
            self._refresh_autoreply_status()

    # ------------------------------------------------------------------
    # 액션/메뉴/툴바
    # ------------------------------------------------------------------

    def _build_actions(self) -> None:
        self.act_fetch_now = QAction("받기", self)
        self.act_fetch_now.setShortcut(QKeySequence("F5"))
        self.act_fetch_now.triggered.connect(self._on_fetch_now)

        self.act_compose_new = QAction("새 메일", self)
        self.act_compose_new.setShortcut(QKeySequence("Ctrl+N"))
        self.act_compose_new.triggered.connect(self._on_compose_new)

        self.act_reply = QAction("회신", self)
        self.act_reply.setShortcut(QKeySequence("Ctrl+R"))
        self.act_reply.triggered.connect(lambda: self._on_reply(reply_all=False))

        self.act_reply_all = QAction("전체회신", self)
        self.act_reply_all.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self.act_reply_all.triggered.connect(lambda: self._on_reply(reply_all=True))

        self.act_forward = QAction("전달", self)
        self.act_forward.setShortcut(QKeySequence("Ctrl+L"))
        self.act_forward.triggered.connect(self._on_forward)

        self.act_delete = QAction("삭제", self)
        self.act_delete.setShortcut(QKeySequence.StandardKey.Delete)
        self.act_delete.triggered.connect(self._on_delete)

        self.act_toggle_read = QAction("읽음토글", self)
        self.act_toggle_read.triggered.connect(self._on_toggle_read)

        self.act_toggle_flag = QAction("플래그", self)
        self.act_toggle_flag.triggered.connect(self._on_toggle_flag)

        self.act_kill_switch = QAction("긴급정지", self)
        # §11.1: 자동회신이 꺼져 있으면 비활성 + "자동회신이 꺼져 있습니다". 켜져 있을 때의 실제
        # 긴급정지 동작(§7.7)은 이후 단계에서 붙인다 — 그때까지는 켜져 있어도 비활성이다.
        self.act_kill_switch.setEnabled(False)
        self.act_kill_switch.setToolTip(KILL_SWITCH_TOOLTIP_OFF)

        self.act_open_accounts = QAction("계정", self)
        self.act_open_accounts.triggered.connect(self._on_open_accounts)

        self.act_open_rules = QAction("규칙 설정", self)
        self.act_open_rules.triggered.connect(self._on_open_rules)

        self.act_open_settings = QAction("설정", self)
        self.act_open_settings.triggered.connect(lambda: self._on_open_settings())

        self.act_claude_panel = QAction("Claude/MCP 패널", self)
        self.act_claude_panel.triggered.connect(self._on_open_mcp_panel)

        self.act_mcp_status = QAction("MCP 서버 상태", self)
        self.act_mcp_status.triggered.connect(self._on_show_mcp_status)

        self.act_pending_approvals = QAction("승인 대기 목록", self)
        self.act_pending_approvals.triggered.connect(self._on_show_pending_approvals)

        self.act_open_manual = QAction("사용 설명서", self)
        self.act_open_manual.setShortcut(QKeySequence("F1"))
        self.act_open_manual.triggered.connect(lambda: self._on_open_manual("00_인덱스"))

        self.act_check_update = QAction("업데이트 확인", self)
        self.act_check_update.triggered.connect(self._on_check_update)

        self.act_open_log_folder = QAction("로그 폴더 열기", self)
        self.act_open_log_folder.triggered.connect(self._on_open_log_folder)

        self.act_about = QAction("정보", self)
        self.act_about.triggered.connect(self._on_show_about)

        self.act_quit = QAction("종료", self)
        self.act_quit.triggered.connect(self.close)

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("파일(&F)")
        file_menu.addAction(self.act_quit)

        menu_bar.addMenu("편집(&E)")

        mail_menu = menu_bar.addMenu("메일(&M)")
        mail_menu.addAction(self.act_fetch_now)
        mail_menu.addSeparator()
        mail_menu.addAction(self.act_compose_new)
        mail_menu.addAction(self.act_reply)
        mail_menu.addAction(self.act_reply_all)
        mail_menu.addAction(self.act_forward)
        mail_menu.addSeparator()
        mail_menu.addAction(self.act_delete)
        mail_menu.addAction(self.act_toggle_read)
        mail_menu.addAction(self.act_toggle_flag)

        tools_menu = menu_bar.addMenu("도구(&T)")
        tools_menu.addAction(self.act_open_accounts)
        tools_menu.addAction(self.act_open_rules)
        tools_menu.addAction(self.act_open_settings)

        claude_menu = menu_bar.addMenu("Claude(&C)")
        claude_menu.addAction(self.act_claude_panel)
        claude_menu.addAction(self.act_mcp_status)
        claude_menu.addSeparator()
        claude_menu.addAction(self.act_pending_approvals)

        help_menu = menu_bar.addMenu("도움말(&H)")
        help_menu.addAction(self.act_open_manual)
        help_menu.addAction(self.act_check_update)
        help_menu.addSeparator()
        help_menu.addAction(self.act_open_log_folder)
        help_menu.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("메인 툴바", self)
        toolbar.setObjectName("mainToolBar")
        for action in (
            self.act_fetch_now,
            self.act_compose_new,
            self.act_reply,
            self.act_reply_all,
            self.act_forward,
            self.act_delete,
            self.act_toggle_read,
            self.act_toggle_flag,
        ):
            toolbar.addAction(action)
        toolbar.addSeparator()

        self._search_edit = QLineEdit(self)
        self._search_edit.setProperty("role", "search")
        self._search_edit.setPlaceholderText("검색")
        self._search_edit.setMaximumWidth(220)
        self._search_edit.returnPressed.connect(self._on_search_changed)
        toolbar.addWidget(self._search_edit)

        self._filter_combo = QComboBox(self)
        self._filter_combo.addItems(_FILTER_LABELS)
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self._filter_combo)

        toolbar.addSeparator()
        toolbar.addAction(self.act_kill_switch)

        self.addToolBar(toolbar)

    # ------------------------------------------------------------------
    # 중앙 위젯(폴더 트리 | 그리드 / 미리보기)
    # ------------------------------------------------------------------

    def _build_central_widget(self) -> None:
        self._folder_model = FolderTreeModel(self)
        self._folder_tree = QTreeView(self)
        self._folder_tree.setObjectName("folderTree")
        self._folder_tree.setHeaderHidden(True)
        self._folder_tree.setModel(self._folder_model)
        self._folder_tree.setColumnWidth(0, 160)
        self._folder_tree.setColumnWidth(1, 56)
        self._folder_tree.selectionModel().selectionChanged.connect(self._on_tree_selection_changed)
        self._folder_tree.doubleClicked.connect(self._on_tree_double_clicked)

        self._message_model = MessageListModel(self)
        self._message_model.set_fetch_more_callback(self._on_grid_fetch_more)
        self._message_model.sortRequested.connect(self._on_sort_requested)

        self._message_grid = QTableView(self)
        self._message_grid.setObjectName("messageGrid")
        self._message_grid.setModel(self._message_model)
        self._message_grid.setAlternatingRowColors(True)
        self._message_grid.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._message_grid.setSortingEnabled(True)
        self._message_grid.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch
        )
        self._message_grid.selectionModel().currentRowChanged.connect(self._on_message_selected)
        self._message_grid.doubleClicked.connect(self._on_grid_double_clicked)

        self._preview = self._build_preview_panel()

        self._right_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._right_splitter.setObjectName("rightSplitter")
        self._right_splitter.addWidget(self._message_grid)
        self._right_splitter.addWidget(self._preview)
        self._right_splitter.setStretchFactor(0, 2)
        self._right_splitter.setStretchFactor(1, 1)

        main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        main_splitter.setObjectName("mainSplitter")
        main_splitter.addWidget(self._folder_tree)
        main_splitter.addWidget(self._right_splitter)
        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 3)

        # §11.1: 툴바 아래, 본문 위에 업데이트 배너 자리를 둔다(평소에는 숨김).
        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        self._update_banner = UpdateBanner(central)
        central_layout.addWidget(self._update_banner)
        central_layout.addWidget(main_splitter, stretch=1)
        self.setCentralWidget(central)

    def _build_preview_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setObjectName("messagePreview")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        # 받은 메일의 제목·보낸사람·받는사람은 공격자가 마음대로 정하는 문자열이다. QLabel
        # 기본값(AutoText)은 `<img src="file://...">`를 리치텍스트로 해석해 메일을 선택만
        # 해도 UNC 경로를 로드할 수 있으므로 반드시 평문으로 표시한다(보안검토 H-3).
        self._preview_from = QLabel("", panel)
        self._preview_from.setTextFormat(Qt.TextFormat.PlainText)
        self._preview_from.setProperty("role", "previewFrom")
        self._preview_meta = QLabel("", panel)
        self._preview_meta.setTextFormat(Qt.TextFormat.PlainText)
        self._preview_meta.setProperty("role", "previewMeta")
        self._preview_subject = QLabel("", panel)
        self._preview_subject.setTextFormat(Qt.TextFormat.PlainText)
        self._preview_subject.setProperty("role", "previewSubject")
        layout.addWidget(self._preview_subject)
        layout.addWidget(self._preview_from)
        layout.addWidget(self._preview_meta)

        button_row = QHBoxLayout()
        self._btn_summary = QPushButton("요약", panel)
        self._btn_summary.setEnabled(False)
        self._btn_summary.setToolTip("이 기능은 P3에서 제공됩니다")
        self._btn_claude_draft = QPushButton("Claude 초안 만들기", panel)
        self._btn_claude_draft.setEnabled(False)
        # 앱이 직접 claude를 실행하는 수동 초안(kind=manual_draft, §3.3)은 P3 범위다.
        # P2에서는 Claude Code가 MCP로 초안을 만든다(Claude 메뉴 > Claude/MCP 패널).
        self._btn_claude_draft.setToolTip(
            "이 기능은 P3에서 제공됩니다. 지금은 Claude Code에서 MCP로 초안을 만들 수 있습니다."
        )
        self._btn_raw_view = QPushButton("원문 보기", panel)
        self._btn_raw_view.clicked.connect(self._on_view_raw_source)
        for btn in (self._btn_summary, self._btn_claude_draft, self._btn_raw_view):
            button_row.addWidget(btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self._attachments_list = QListWidget(panel)
        self._attachments_list.setMaximumHeight(90)
        self._attachments_list.setVisible(False)
        self._attachments_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._attachments_list.customContextMenuRequested.connect(self._on_attachment_context_menu)
        self._attachments_list.itemDoubleClicked.connect(self._on_attachment_save)
        layout.addWidget(self._attachments_list)

        # "작업 > 승인 대기" 가상 폴더(§11.1 C-04): 목록 + [승인 창 열기] [취소].
        self._pending_panel = QWidget(panel)
        pending_layout = QVBoxLayout(self._pending_panel)
        pending_layout.setContentsMargins(0, 0, 0, 0)
        self._pending_list = QListWidget(self._pending_panel)
        self._pending_list.itemDoubleClicked.connect(self._on_pending_open)
        pending_layout.addWidget(self._pending_list)
        pending_buttons = QHBoxLayout()
        self._btn_pending_open = QPushButton("승인 창 열기", self._pending_panel)
        self._btn_pending_open.setProperty("variant", "primary")
        self._btn_pending_open.clicked.connect(self._on_pending_open)
        self._btn_pending_cancel = QPushButton("취소(초안으로)", self._pending_panel)
        self._btn_pending_cancel.setProperty("variant", "secondary")
        self._btn_pending_cancel.clicked.connect(self._on_pending_cancel)
        pending_buttons.addWidget(self._btn_pending_open)
        pending_buttons.addWidget(self._btn_pending_cancel)
        pending_buttons.addStretch(1)
        pending_layout.addLayout(pending_buttons)
        self._pending_panel.setVisible(False)
        layout.addWidget(self._pending_panel)

        self._preview_body = SecureMailBrowser(panel)
        self._preview_body.setPlaceholderText("메일을 선택하면 여기에 미리보기가 표시됩니다.")
        layout.addWidget(self._preview_body, stretch=1)

        return panel

    def _build_status_bar(self) -> None:
        status_bar = QStatusBar(self)
        self._mail_status_label = QLabel("메일 ○계정 없음", self)
        self._mcp_status_label = QLabel("MCP ●확인 중", self)
        self._claude_status_label = QLabel("Claude ●미연결", self)
        self._job_status_label = QLabel("잡 대기 0", self)
        self._job_status_label.setToolTip("자동회신 잡 대기·실행 중 건수(Claude/MCP 패널 > 잡)")
        # §11.1 자동회신 토글 연동: off면 "자동회신 ○꺼짐", on이면 "자동회신 ●켜짐".
        # 클릭하면 설정 > 자동회신 탭을 연다. 값은 AutoReplyEnabledChanged 이벤트로 갱신한다.
        self._autosend_status_label = _ClickableLabel("자동회신 ○꺼짐", self)
        self._autosend_status_label.setProperty("statusDot", "connecting")
        self._autosend_status_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autosend_status_label.setToolTip("클릭하면 설정 > 자동회신 탭을 엽니다")
        self._autosend_status_label.clicked.connect(
            lambda: self._on_open_settings(initial_tab=TAB_AUTOREPLY)
        )
        self._version_label = QLabel(f"EmailToMCP v{self._version}", self)
        self._version_label.setObjectName("versionLabel")
        self._mcp_status_label.setProperty("statusDot", "disconnected")
        self._claude_status_label.setProperty("statusDot", "disconnected")
        for label in (
            self._mail_status_label,
            self._mcp_status_label,
            self._claude_status_label,
            self._job_status_label,
            self._autosend_status_label,
        ):
            status_bar.addWidget(label)
        # §11.8 "새 버전 있음 → 상태줄 배지". 표시할 것이 없으면 숨긴다.
        self._update_status_label = QLabel("", self)
        self._update_status_label.setVisible(False)
        status_bar.addPermanentWidget(self._update_status_label)
        status_bar.addPermanentWidget(self._version_label)
        # 상태줄 문자열에는 오류 메시지·업데이트 서버 응답 등 외부 문자열이 섞일 수 있다.
        # 리치텍스트로 해석되지 않게 전부 평문으로 고정한다(보안검토 H-3, 심층 방어).
        for label in (
            self._mail_status_label,
            self._mcp_status_label,
            self._claude_status_label,
            self._job_status_label,
            self._autosend_status_label,
            self._update_status_label,
            self._version_label,
        ):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.setStatusBar(status_bar)

    def _build_tray_icon(self) -> None:
        self._tray_icon: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(_load_icon(), self)
        tray.setToolTip("EmailToMCP")
        tray.messageClicked.connect(self._on_tray_message_clicked)
        tray.show()
        self._tray_icon = tray

    # ------------------------------------------------------------------
    # 트리 로딩
    # ------------------------------------------------------------------

    def _reload_tree(self) -> None:
        if self._api is None or self._bridge is None:
            return
        try:
            nodes = self._bridge.call_backend(self._api.load_tree(), timeout=10.0)
        except Exception:
            logger.warning("폴더 트리 조회 실패", exc_info=True)
            return

        selected_folder = self._current_folder_id
        selected_virtual = self._current_virtual_kind

        self._folder_model.clear_tree()
        self._folder_account_map.clear()
        total_outbox = 0
        total_pending = 0

        for node in nodes:
            account = node.account
            account_item = self._folder_model.add_account(account.id, account.display_name)
            status, tooltip = self._account_status.get(account.id, ("connecting", ""))
            self._folder_model.set_account_status(account.id, status, tooltip)
            for folder_node in node.folders:
                folder = folder_node.folder
                self._folder_account_map[folder.id] = account.id
                self._folder_model.add_folder(
                    account_item,
                    folder.id,
                    _folder_label(folder.role, folder.name),
                    unread=folder_node.unread_count,
                )
            total_outbox += node.outbox_count
            total_pending += node.pending_approval_count

        work_group = self._folder_model.add_group("작업")
        self._folder_model.add_virtual_folder(
            work_group, "outbox", "보낼편지함", count=total_outbox
        )
        self._folder_model.add_virtual_folder(
            work_group, "pending_approval", "승인 대기", count=total_pending
        )

        self._folder_tree.expandAll()
        self._update_mail_status_label()

        # 이전에 선택했던 폴더/가상폴더가 여전히 있으면 그리드를 다시 그린다.
        if selected_folder is not None and selected_folder in self._folder_account_map:
            self._reload_grid(reset=True)
        elif selected_virtual is not None:
            self._reload_virtual_grid()

    def _apply_preview_position(self) -> None:
        if self._api is None or self._bridge is None:
            return
        try:
            position = self._bridge.call_backend(
                self._api.get_setting("preview_position", "bottom"), timeout=3.0
            )
        except Exception:
            return
        orientation = Qt.Orientation.Horizontal if position == "right" else Qt.Orientation.Vertical
        self._right_splitter.setOrientation(orientation)

    def _update_mail_status_label(self) -> None:
        account_ids = set(self._folder_account_map.values())
        total = len(account_ids)
        if total == 0:
            self._mail_status_label.setText("메일 ○계정 없음")
            self._mail_status_label.setProperty("statusDot", "connecting")
        else:
            errors = [
                aid
                for aid in account_ids
                if self._account_status.get(aid, ("connecting", ""))[0] == "disconnected"
            ]
            if errors:
                self._mail_status_label.setText(f"메일 ●오류({len(errors)}/{total}계정)")
                self._mail_status_label.setProperty("statusDot", "disconnected")
            else:
                self._mail_status_label.setText(f"메일 ●정상({total}계정)")
                self._mail_status_label.setProperty("statusDot", "connected")
        self._mail_status_label.style().unpolish(self._mail_status_label)
        self._mail_status_label.style().polish(self._mail_status_label)

    # ------------------------------------------------------------------
    # 트리 선택 → 그리드 로딩
    # ------------------------------------------------------------------

    def _on_tree_selection_changed(self, *_args: object) -> None:
        indexes = self._folder_tree.selectionModel().selectedIndexes()
        if not indexes:
            return
        index = indexes[0]
        folder_id = self._folder_model.folder_id_at(index)
        virtual_kind = self._folder_model.virtual_kind_at(index)
        account_id = self._folder_model.account_id_at(index)

        if folder_id is not None:
            self._current_folder_id = folder_id
            self._current_virtual_kind = None
            self._current_account_id = self._folder_account_map.get(folder_id)
            self._pending_panel.setVisible(False)
            self._reload_grid(reset=True)
        elif virtual_kind is not None:
            self._current_folder_id = None
            self._current_virtual_kind = virtual_kind
            self._reload_virtual_grid()
        elif account_id is not None:
            self._current_account_id = account_id

    def _on_tree_double_clicked(self, index: QModelIndex) -> None:
        del index  # 더블클릭으로 트리 노드 이름 편집은 지원하지 않는다(읽기 전용 트리).

    def _reload_grid(self, *, reset: bool) -> None:
        if self._api is None or self._bridge is None or self._current_folder_id is None:
            return
        offset = 0 if reset else self._message_model.row_count_loaded()
        try:
            rows, total = self._bridge.call_backend(
                self._api.list_messages(
                    self._current_folder_id,
                    offset=offset,
                    limit=PAGE_SIZE,
                    sort_column=self._sort_column,
                    descending=self._sort_descending,
                    unread_only=self._filter_unread,
                    flagged_only=self._filter_flagged,
                    attachments_only=self._filter_attachments,
                    search_text=self._search_text,
                ),
                timeout=10.0,
            )
        except Exception:
            logger.warning("메일 목록 조회 실패", exc_info=True)
            return
        if reset:
            self._message_model.reset_rows(rows, total)
        else:
            self._message_model.append_rows(rows)

    def _on_grid_fetch_more(self, offset: int) -> None:
        if self._api is None or self._bridge is None or self._current_folder_id is None:
            return
        try:
            rows, _total = self._bridge.call_backend(
                self._api.list_messages(
                    self._current_folder_id,
                    offset=offset,
                    limit=PAGE_SIZE,
                    sort_column=self._sort_column,
                    descending=self._sort_descending,
                    unread_only=self._filter_unread,
                    flagged_only=self._filter_flagged,
                    attachments_only=self._filter_attachments,
                    search_text=self._search_text,
                ),
                timeout=10.0,
            )
        except Exception:
            logger.warning("메일 목록 추가 조회 실패", exc_info=True)
            return
        self._message_model.append_rows(rows)

    def _reload_virtual_grid(self) -> None:
        if self._current_virtual_kind == "pending_approval":
            self._reload_pending_grid()
        else:
            self._reload_outbox_grid()

    def _reload_pending_grid(self) -> None:
        """작업 > 승인 대기(§11.1): MCP 발송 승인을 기다리는 초안 목록."""
        lister = getattr(self._api, "list_pending_approvals", None)
        if lister is None or self._bridge is None:
            return
        try:
            drafts = self._bridge.call_backend(lister(), timeout=10.0)
        except Exception:
            logger.warning("승인 대기 목록 조회 실패", exc_info=True)
            return
        self._message_model.reset_rows([], 0)
        self._pending_list.clear()
        for draft in drafts:
            recipients = ", ".join([*draft.to_addrs, *draft.cc_addrs, *draft.bcc_addrs])
            item = QListWidgetItem(f"#{draft.id} {draft.subject or '(제목 없음)'} → {recipients}")
            item.setData(Qt.ItemDataRole.UserRole, draft.id)
            self._pending_list.addItem(item)
        self._pending_panel.setVisible(True)
        self._preview_body.setPlainText(
            "Claude가 발송을 요청해 사용자 승인을 기다리는 메일입니다. 항목을 더블클릭하거나 "
            "[승인 창 열기]로 내용을 확인한 뒤 승인/거부하세요. 24시간이 지나면 초안으로 "
            "돌아갑니다."
            if drafts
            else "승인 대기 중인 메일이 없습니다."
        )

    def _selected_pending_draft_id(self) -> int | None:
        item = self._pending_list.currentItem()
        if item is None and self._pending_list.count() == 1:
            item = self._pending_list.item(0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_pending_open(self, *_args: object) -> None:
        draft_id = self._selected_pending_draft_id()
        if draft_id is not None:
            self._open_approval_dialog(draft_id, activate=True)

    def _on_pending_cancel(self) -> None:
        draft_id = self._selected_pending_draft_id()
        if draft_id is None or self._api is None or self._bridge is None:
            return
        try:
            self._bridge.call_backend(self._api.reject_draft(draft_id), timeout=5.0)
        except Exception as exc:
            plain_warning(self, "승인 대기", f"취소하지 못했습니다: {exc}")
        self._reload_tree()

    def _reload_outbox_grid(self) -> None:
        if self._api is None or self._bridge is None:
            return
        self._pending_panel.setVisible(False)
        try:
            drafts = self._bridge.call_backend(self._api.list_outbox_drafts(), timeout=10.0)
        except Exception:
            logger.warning("보낼편지함 조회 실패", exc_info=True)
            return
        self._message_model.reset_rows([], 0)
        self._preview_body.setPlainText(
            "\n".join(
                f"#{d.id} [{_OUTBOX_STATUS_LABELS.get(d.status, d.status)}] "
                f"{d.subject or '(제목 없음)'} → {', '.join(d.to_addrs)}"
                + (f" — {d.last_error}" if d.last_error else "")
                for d in drafts
            )
            or "보낼편지함이 비어 있습니다."
        )

    def _on_sort_requested(self, sort_column: str, descending: bool) -> None:
        self._sort_column = sort_column
        self._sort_descending = descending
        self._reload_grid(reset=True)

    def _on_search_changed(self) -> None:
        self._search_text = self._search_edit.text().strip()
        self._reload_grid(reset=True)

    def _on_filter_changed(self, index: int) -> None:
        self._filter_unread = index == 1
        self._filter_flagged = index == 2
        self._filter_attachments = index == 3
        self._reload_grid(reset=True)

    # ------------------------------------------------------------------
    # 미리보기
    # ------------------------------------------------------------------

    def _on_message_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        row = self._message_model.row_at(current.row())
        if row is None:
            return
        self._load_preview(row.id)
        if not row.is_read and self._api is not None and self._bridge is not None:
            try:
                self._bridge.call_backend(self._api.set_read(row.id, True), timeout=5.0)
            except Exception:
                logger.warning("읽음 처리 실패", exc_info=True)
            self._reload_tree()

    def _load_preview(self, message_id: int) -> None:
        if self._api is None or self._bridge is None:
            return
        try:
            detail = self._bridge.call_backend(
                self._api.get_message_detail(message_id), timeout=5.0
            )
        except Exception:
            logger.warning("메일 상세 조회 실패", exc_info=True)
            return
        if detail is None:
            return
        row = detail.message
        self._preview_subject.setText(row.subject or "(제목 없음)")
        from_display = row.from_name or row.from_addr or ""
        self._preview_from.setText(f"{from_display} <{row.from_addr or ''}>")
        to_display = ", ".join(_safe_json_list(row.to_addrs_json))
        self._preview_meta.setText(f"받는사람: {to_display}\n날짜: {row.received_at}")
        self._preview_body.setPlainText(row.body_text or "")
        self._preview_body.moveCursor(QTextCursor.MoveOperation.Start)

        self._attachments_list.clear()
        self._attachments_list.setVisible(bool(detail.attachments))
        for att in detail.attachments:
            if att.is_inline:
                continue
            label = f"{att.filename_safe or att.filename_raw or '(이름 없음)'}"
            if att.size_bytes:
                label += f" ({att.size_bytes // 1024}KB)"
            if att.is_dangerous:
                label += " ⚠ 실행 위험 확장자"
            item = QListWidgetItem(label)
            item.setData(
                Qt.ItemDataRole.UserRole,
                (message_id, att.part_index, att.filename_safe, att.is_dangerous),
            )
            self._attachments_list.addItem(item)

    def _on_attachment_context_menu(self, pos) -> None:  # noqa: ANN001 — QPoint
        item = self._attachments_list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        save_action = menu.addAction("저장")
        open_action = menu.addAction("열기")
        chosen = menu.exec(self._attachments_list.mapToGlobal(pos))
        if chosen is save_action:
            self._save_attachment_item(item)
        elif chosen is open_action:
            self._open_attachment_item(item)

    def _on_attachment_save(self, item: QListWidgetItem) -> None:
        self._save_attachment_item(item)

    def _save_attachment_item(self, item: QListWidgetItem) -> None:
        if self._api is None or self._bridge is None:
            return
        message_id, part_index, _filename, _dangerous = item.data(Qt.ItemDataRole.UserRole)
        dest_dir = QFileDialog.getExistingDirectory(self, "첨부 저장 위치 선택")
        if not dest_dir:
            return
        try:
            saved_path = self._bridge.call_backend(
                self._api.save_attachment(message_id, part_index, dest_dir), timeout=15.0
            )
        except Exception as exc:
            plain_warning(self, "첨부 저장", f"저장하지 못했습니다: {exc}")
            return
        plain_information(self, "첨부 저장", f"저장했습니다: {saved_path}")

    def _open_attachment_item(self, item: QListWidgetItem) -> None:
        if self._api is None or self._bridge is None:
            return
        message_id, part_index, _filename, is_dangerous = item.data(Qt.ItemDataRole.UserRole)
        if is_dangerous:
            QMessageBox.warning(
                self,
                "첨부 열기",
                "실행 위험이 있는 확장자라서 바로 열 수 없습니다. [저장] 후 직접 확인하세요.",
            )
            return
        temp_dir = tempfile.gettempdir()
        try:
            saved_path = self._bridge.call_backend(
                self._api.save_attachment(message_id, part_index, temp_dir), timeout=15.0
            )
        except Exception as exc:
            plain_warning(self, "첨부 열기", f"열지 못했습니다: {exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(saved_path))

    def _on_view_raw_source(self) -> None:
        index = self._message_grid.currentIndex()
        row = self._message_model.row_at(index.row())
        if row is None or self._api is None or self._bridge is None:
            return
        try:
            raw = self._bridge.call_backend(self._api.get_raw_eml(row.id), timeout=5.0)
        except Exception as exc:
            plain_warning(self, "원문 보기", f"원문을 불러오지 못했습니다: {exc}")
            return
        viewer = RawSourceViewer(raw, parent=self)
        viewer.exec()

    # ------------------------------------------------------------------
    # 메일 동작(받기/작성/회신/전달/삭제/읽음/플래그)
    # ------------------------------------------------------------------

    def _on_fetch_now(self) -> None:
        if self._api is None or self._bridge is None:
            return
        self.statusBar().showMessage("수신 중...")
        async_utils.run_async(
            self._bridge, self._api.poll_all(), self._on_fetch_now_done, parent=self
        )

    def _on_fetch_now_done(self, future) -> None:  # noqa: ANN001 — concurrent.futures.Future
        try:
            future.result()
        except Exception as exc:
            self.statusBar().showMessage(f"수신 실패: {exc}", 5000)
            return
        self.statusBar().showMessage("수신 완료", 3000)
        self._reload_tree()
        if self._current_folder_id is not None:
            self._reload_grid(reset=True)

    def _current_message_row(self):
        index = self._message_grid.currentIndex()
        return self._message_model.row_at(index.row())

    def _on_compose_new(self) -> None:
        if self._api is None or self._bridge is None or self._current_account_id is None:
            QMessageBox.information(self, "새 메일", "먼저 왼쪽 트리에서 계정/폴더를 선택하세요.")
            return
        window = ComposeWindow.open_new(
            self._current_account_id, api=self._api, bridge=self._bridge, parent=self
        )
        window.show()

    def _on_reply(self, *, reply_all: bool) -> None:
        row = self._current_message_row()
        if row is None or self._api is None or self._bridge is None:
            return
        window = ComposeWindow.open_reply(
            row.account_id,
            row.id,
            reply_all=reply_all,
            api=self._api,
            bridge=self._bridge,
            parent=self,
        )
        window.show()

    def _on_forward(self) -> None:
        row = self._current_message_row()
        if row is None or self._api is None or self._bridge is None:
            return
        mode = ComposeWindow.ask_forward_mode(self)
        if mode is None:
            return
        window = ComposeWindow.open_forward(
            row.account_id, row.id, mode=mode, api=self._api, bridge=self._bridge, parent=self
        )
        window.show()

    def _on_delete(self) -> None:
        row = self._current_message_row()
        if row is None or self._api is None or self._bridge is None:
            return
        try:
            self._bridge.call_backend(self._api.move_to_trash(row.id), timeout=5.0)
        except Exception as exc:
            plain_warning(self, "삭제", f"삭제하지 못했습니다: {exc}")
            return
        self._reload_tree()
        self._reload_grid(reset=True)

    def _on_toggle_read(self) -> None:
        row = self._current_message_row()
        if row is None or self._api is None or self._bridge is None:
            return
        try:
            self._bridge.call_backend(self._api.set_read(row.id, not row.is_read), timeout=5.0)
        except Exception:
            logger.warning("읽음 토글 실패", exc_info=True)
            return
        self._reload_tree()
        self._reload_grid(reset=True)

    def _on_toggle_flag(self) -> None:
        row = self._current_message_row()
        if row is None or self._api is None or self._bridge is None:
            return
        try:
            self._bridge.call_backend(
                self._api.set_flagged(row.id, not row.is_flagged), timeout=5.0
            )
        except Exception:
            logger.warning("플래그 토글 실패", exc_info=True)
            return
        self._reload_grid(reset=True)

    def _on_grid_double_clicked(self, index: QModelIndex) -> None:
        del index  # 더블클릭 = 선택과 동일(별도 창 미리보기는 P4)

    # ------------------------------------------------------------------
    # 도구 메뉴(계정/규칙/설정), 도움말
    # ------------------------------------------------------------------

    def _on_open_accounts(self) -> None:
        if self._api is None or self._bridge is None:
            QMessageBox.information(self, "계정", "백엔드가 아직 준비되지 않았습니다.")
            return
        dialog = AccountManagerDialog(api=self._api, bridge=self._bridge, parent=self)
        dialog.exec()
        self._reload_tree()

    def _on_open_rules(self) -> None:
        """도구 > 규칙 설정(§11.4). 전역 토글이 꺼져 있어도 편집·드라이런은 된다(§7.0)."""
        if self._api is None or self._bridge is None:
            QMessageBox.information(self, "규칙 설정", "백엔드가 아직 준비되지 않았습니다.")
            return
        dialog = RuleManagerDialog(
            api=self._api,
            bridge=self._bridge,
            open_settings=lambda: self._on_open_settings(TAB_AUTOREPLY),
            parent=self,
        )
        dialog.exec()

    def _on_open_settings(self, initial_tab: str | None = None) -> None:
        if self._api is None or self._bridge is None:
            QMessageBox.information(self, "설정", "백엔드가 아직 준비되지 않았습니다.")
            return
        dialog = SettingsDialog(
            api=self._api,
            bridge=self._bridge,
            apply_theme=self._apply_theme,
            initial_tab=initial_tab,
            parent=self,
        )
        dialog.exec()
        self._apply_preview_position()
        self._refresh_autoreply_status()

    # ------------------------------------------------------------------
    # 자동회신 토글 표시(§11.1, §7.0)
    # ------------------------------------------------------------------

    def _refresh_autoreply_status(self) -> None:
        """상태줄 자동회신 표시와 [긴급정지] 활성 여부를 DB 유효값으로 다시 그린다.

        이벤트 payload를 그대로 믿지 않고 백엔드 스냅샷을 다시 읽는다(업데이트 표시와 같은 방식).
        읽지 못하면 꺼짐으로 표시한다(fail-closed).
        """
        enabled = False
        getter = getattr(self._api, "get_autoreply_toggle", None)
        if getter is not None and self._bridge is not None:
            try:
                view = self._bridge.call_backend(getter())
                enabled = bool(isinstance(view, dict) and view.get("enabled"))
            except Exception:  # noqa: BLE001 — 표시 실패는 꺼짐으로 둔다
                logger.warning("자동회신 상태 조회 실패", exc_info=True)
        self.apply_autoreply_status(enabled)

    def apply_autoreply_status(self, enabled: bool) -> None:
        label = self._autosend_status_label
        if enabled:
            label.setText("자동회신 ●켜짐")
            label.setProperty("statusDot", "connected")
            # 실제 긴급정지(§7.7)는 이후 단계 — 그때까지는 켜져 있어도 비활성으로 둔다.
            self.act_kill_switch.setEnabled(False)
            self.act_kill_switch.setToolTip(KILL_SWITCH_TOOLTIP_PENDING)
        else:
            label.setText("자동회신 ○꺼짐")
            label.setProperty("statusDot", "connecting")
            self.act_kill_switch.setEnabled(False)
            self.act_kill_switch.setToolTip(KILL_SWITCH_TOOLTIP_OFF)
        label.style().unpolish(label)
        label.style().polish(label)
        self._refresh_job_count()

    def _refresh_job_count(self) -> None:
        """상태줄 "잡 대기 N"(§11.1): 자동회신 잡 대기+실행 중 건수. 읽지 못하면 그대로 둔다."""
        getter = getattr(self._api, "get_autoreply_queue_status", None)
        if getter is None or self._bridge is None:
            return
        try:
            status = self._bridge.call_backend(getter(), timeout=3.0)
        except Exception:  # noqa: BLE001
            logger.debug("자동회신 잡 건수 조회 실패", exc_info=True)
            return
        counts = status.get("counts") if isinstance(status, dict) else None
        if isinstance(counts, dict):
            waiting = int(counts.get("queued", 0)) + int(counts.get("running", 0))
            self._job_status_label.setText(f"잡 대기 {waiting}")

    def _on_open_manual(self, section: str) -> None:
        viewer = ManualViewer(parent=self)
        viewer.show_section(section)
        viewer.exec()

    def _on_open_log_folder(self) -> None:
        """도움말 > 로그 폴더 열기 — 디버깅 로그(최근 7일분) 폴더를 파일탐색기로 연다."""
        if self._api is None or self._bridge is None:
            plain_information(self, "로그 폴더", "백엔드가 아직 준비되지 않았습니다.")
            return
        try:
            log_dir = self._bridge.call_backend(self._api.log_dir_path(), timeout=5.0)
        except Exception as exc:
            plain_warning(self, "로그 폴더", f"로그 폴더 경로를 가져오지 못했습니다: {exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir)):
            plain_warning(self, "로그 폴더", f"로그 폴더를 열지 못했습니다:\n{log_dir}")

    def _on_show_about(self) -> None:
        QMessageBox.about(
            self,
            "EmailToMCP 정보",
            f"EmailToMCP v{self._version}\nUTInfo — 로컬 메일 클라이언트 + 내장 MCP 서버",
        )

    # ------------------------------------------------------------------
    # 백엔드 이벤트 수신(core.events → qt_bridge → Signal)
    # ------------------------------------------------------------------

    def _on_backend_event(self, name: str, payload: object) -> None:
        handler = {
            "MessageReceived": self._handle_message_received,
            "AccountConnected": lambda p: self._handle_account_status(p, "connected"),
            "AccountError": lambda p: self._handle_account_status(p, "disconnected"),
            "SendCompleted": self._handle_send_event,
            "SendFailed": self._handle_send_event,
            # MCP 서버 상태 / Claude(대화형 클라이언트) 연결 상태(§4.2, §11.1 상태줄).
            "McpStatusChanged": self._handle_mcp_status_changed,
            "McpClientConnected": lambda _p: self._set_claude_status(True),
            "McpClientDisconnected": lambda _p: self._set_claude_status(False),
            # MCP 발송 승인(§11.6).
            "ApprovalRequested": self._handle_approval_requested,
            "ApprovalResolved": self._handle_approval_resolved,
            "DraftChanged": lambda _p: self._handle_drafts_changed(),
            # 업데이트(§11.1 배너, §11.8). payload를 그대로 믿지 않고 백엔드에서 링크를
            # 재검증한 스냅샷을 다시 읽어 그린다.
            "UpdateAvailable": lambda _p: self._refresh_update_status(),
            "UpdateStatusChanged": lambda _p: self._refresh_update_status(),
            "SecurityAlert": self._handle_security_alert,
            # 자동회신 전역 토글(§7.0, §11.1 상태줄).
            "AutoReplyEnabledChanged": lambda _p: self._refresh_autoreply_status(),
            # 자동회신 잡이 끝나면 잡 건수·그리드 "자동회신" 열을 갱신한다(P3 Phase B).
            "JobFinished": lambda _p: self._handle_job_finished(),
        }.get(name)
        if handler is not None:
            handler(payload)

    def _handle_job_finished(self) -> None:
        # 그리드는 다시 읽지 않는다(선택·스크롤 유지). "자동회신" 열은 폴더를 다시 열 때 갱신되고,
        # 만들어진 초안은 임시보관함 건수로 바로 보이도록 트리만 갱신한다.
        self._refresh_job_count()
        self._reload_tree()

    def _handle_message_received(self, payload: object) -> None:
        self._reload_tree()
        if isinstance(payload, dict) and payload.get("folder_id") == self._current_folder_id:
            self._reload_grid(reset=True)
        self._maybe_notify_new_mail(payload)

    def _handle_account_status(self, payload: object, status: str) -> None:
        if not isinstance(payload, dict):
            return
        account_id = payload.get("account_id")
        if account_id is None:
            return
        tooltip = payload.get("error", "") if status == "disconnected" else ""
        self._account_status[account_id] = (status, tooltip)
        self._folder_model.set_account_status(account_id, status, tooltip)
        self._update_mail_status_label()

    def _handle_send_event(self, _payload: object) -> None:
        self._reload_tree()
        if self._current_virtual_kind == "outbox":
            self._reload_outbox_grid()

    def _handle_mcp_status_changed(self, payload: object) -> None:
        """MCP 서버 켜짐/꺼짐/포트 충돌(§11.1 상태줄, §11.5, C-05)."""
        if not isinstance(payload, dict):
            return
        port = payload.get("port")
        error = payload.get("error")
        if payload.get("running"):
            self._mcp_status_label.setText(f"MCP ●서버:{port}")
            self._mcp_status_label.setProperty("statusDot", "connected")
            self._mcp_status_label.setToolTip(f"127.0.0.1:{port}에서 실행 중")
        elif error:
            self._mcp_status_label.setText(f"MCP ●꺼짐(포트 {port} 사용 불가)")
            self._mcp_status_label.setProperty("statusDot", "disconnected")
            self._mcp_status_label.setToolTip(
                f"{error}\n포트는 자동으로 바뀌지 않습니다. 도구 > 설정 > MCP에서 포트를 바꾼 뒤 "
                "앱을 다시 시작하세요."
            )
            self.statusBar().showMessage(f"MCP 서버를 시작하지 못했습니다: {error}", 15000)
        else:
            self._mcp_status_label.setText("MCP ●꺼짐")
            self._mcp_status_label.setProperty("statusDot", "disconnected")
            self._mcp_status_label.setToolTip("")
        self._mcp_status_label.style().unpolish(self._mcp_status_label)
        self._mcp_status_label.style().polish(self._mcp_status_label)

    def _set_claude_status(self, connected: bool) -> None:
        if connected:
            self._claude_status_label.setText("Claude ●연결됨")
            self._claude_status_label.setProperty("statusDot", "connected")
        else:
            self._claude_status_label.setText("Claude ●미연결")
            self._claude_status_label.setProperty("statusDot", "disconnected")
        self._claude_status_label.style().unpolish(self._claude_status_label)
        self._claude_status_label.style().polish(self._claude_status_label)

    # ------------------------------------------------------------------
    # Claude 메뉴 / MCP 발송 승인(§11.5, §11.6)
    # ------------------------------------------------------------------

    def _on_open_mcp_panel(self) -> None:
        if self._api is None or self._bridge is None:
            QMessageBox.information(self, "Claude/MCP 패널", "백엔드가 아직 준비되지 않았습니다.")
            return
        McpPanelDialog(api=self._api, bridge=self._bridge, parent=self).exec()

    def _on_show_mcp_status(self) -> None:
        getter = getattr(self._api, "mcp_status", None)
        if getter is None or self._bridge is None:
            QMessageBox.information(self, "MCP 서버 상태", "백엔드가 아직 준비되지 않았습니다.")
            return
        try:
            status = self._bridge.call_backend(getter(), timeout=3.0)
        except Exception as exc:
            plain_warning(self, "MCP 서버 상태", f"상태를 확인하지 못했습니다: {exc}")
            return
        if status.get("running"):
            text = f"MCP 서버가 127.0.0.1:{status.get('port')}에서 실행 중입니다."
        else:
            text = f"MCP 서버가 꺼져 있습니다. {status.get('error') or ''}".strip()
        text += "\nClaude 연결: " + ("연결됨" if status.get("client_connected") else "미연결")
        plain_information(self, "MCP 서버 상태", text)

    def _on_show_pending_approvals(self) -> None:
        index = self._folder_model.virtual_index("pending_approval")
        if index.isValid():
            self._folder_tree.setCurrentIndex(index)
        else:
            self._current_folder_id = None
            self._current_virtual_kind = "pending_approval"
            self._reload_pending_grid()

    def _handle_approval_requested(self, payload: object) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("draft_id"), int):
            return
        draft_id = payload["draft_id"]
        self._reload_tree()
        # M5: 포커스를 빼앗지 않는다 — OS 알림 + 작업표시줄 깜빡임, 창은 비활성 상태로 띄운다.
        if self._tray_icon is not None:
            self._tray_approval_draft_id = draft_id
            self._tray_icon.showMessage(
                "Claude 발송 승인 요청",
                "Claude가 메일 발송을 요청했습니다. 클릭해서 내용을 확인하세요.",
                QSystemTrayIcon.MessageIcon.Warning,
            )
        QApplication.alert(self)
        self._open_approval_dialog(draft_id, activate=False)

    def _handle_approval_resolved(self, payload: object) -> None:
        if isinstance(payload, dict):
            dialog = self._approval_dialogs.get(payload.get("draft_id"))  # type: ignore[arg-type]
            if dialog is not None:
                dialog.handle_resolved_elsewhere()
        self._handle_drafts_changed()

    def _handle_drafts_changed(self) -> None:
        self._reload_tree()
        if self._current_virtual_kind is not None:
            self._reload_virtual_grid()

    def _on_tray_message_clicked(self) -> None:
        draft_id, self._tray_approval_draft_id = self._tray_approval_draft_id, None
        if draft_id is not None:
            self._open_approval_dialog(draft_id, activate=True)
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _open_approval_dialog(self, draft_id: int, *, activate: bool) -> None:
        if self._api is None or self._bridge is None:
            return
        existing = self._approval_dialogs.get(draft_id)
        if existing is not None:
            if activate:
                existing.raise_()
                existing.activateWindow()
            return
        dialog = SendApprovalDialog(
            draft_id=draft_id,
            api=self._api,
            bridge=self._bridge,
            on_edit_requested=self._open_compose_for_draft,
            parent=self,
        )
        dialog.finished.connect(lambda _result, d=draft_id: self._on_approval_dialog_closed(d))
        self._approval_dialogs[draft_id] = dialog
        dialog.show()
        if activate:
            dialog.raise_()
            dialog.activateWindow()

    def _on_approval_dialog_closed(self, draft_id: int) -> None:
        self._approval_dialogs.pop(draft_id, None)
        self._handle_drafts_changed()

    def _open_compose_for_draft(self, account_id: int, draft_id: int) -> None:
        """[수정 후 발송] — 승인 요청이 취소된 초안을 작성 창으로 연다(사용자가 직접 보냄)."""
        if self._api is None or self._bridge is None:
            return
        window = ComposeWindow.open_existing(
            account_id, draft_id, api=self._api, bridge=self._bridge, parent=self
        )
        window.show()

    # ------------------------------------------------------------------
    # 업데이트 배너/배지(§11.1, §11.8)
    # ------------------------------------------------------------------

    def _refresh_update_status(self) -> None:
        getter = getattr(self._api, "get_update_status", None)
        if getter is None or self._bridge is None:
            return
        try:
            view = self._bridge.call_backend(getter(), timeout=5.0)
        except Exception:
            logger.warning("업데이트 상태 조회 실패", exc_info=True)
            return
        self.apply_update_status(view)

    def apply_update_status(self, view: dict | None) -> None:
        """상태 dict로 배너와 상태줄 배지를 다시 그린다."""
        self._update_banner.apply_status(view)
        badge = status_badge(view)
        if badge is None:
            self._update_status_label.setVisible(False)
            return
        text, dot = badge
        self._update_status_label.setText(text)
        self._update_status_label.setProperty("statusDot", dot)
        self._update_status_label.setToolTip(describe_update_status(view))
        self._update_status_label.style().unpolish(self._update_status_label)
        self._update_status_label.style().polish(self._update_status_label)
        self._update_status_label.setVisible(True)

    def _handle_security_alert(self, payload: object) -> None:
        """§11.8 보안 경보: 경보 다이얼로그 1회(비모달) + 업데이트 자동 확인 정지(백엔드가 처리)."""
        if not isinstance(payload, dict) or payload.get("source") != "update":
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("업데이트 보안 경보")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            "업데이트 정보의 서명 또는 해시 검증에 실패했습니다.\n"
            "위조된 업데이트일 수 있어 업데이트 자동 확인을 정지했습니다.\n"
            "어떤 파일도 내려받거나 설치하지 않았습니다.\n\n"
            f"사유: {payload.get('reason') or '(알 수 없음)'}\n\n"
            "도구 > 설정 > 업데이트에서 상태를 확인할 수 있습니다."
        )
        box.setModal(False)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.show()
        self._refresh_update_status()

    def _on_check_update(self) -> None:
        """도움말 > 업데이트 확인 — 수동 확인 1회(UI를 막지 않음)."""
        checker = getattr(self._api, "check_update_now", None)
        if checker is None or self._bridge is None:
            QMessageBox.information(self, "업데이트 확인", "백엔드가 아직 준비되지 않았습니다.")
            return
        self.statusBar().showMessage("업데이트 확인 중...")
        async_utils.run_async(self._bridge, checker(), self._on_check_update_done, parent=self)

    def _on_check_update_done(self, future) -> None:  # noqa: ANN001 — concurrent.futures.Future
        self.statusBar().clearMessage()
        try:
            view = future.result()
        except Exception as exc:
            plain_warning(self, "업데이트 확인", f"확인하지 못했습니다: {exc}")
            return
        self.apply_update_status(view)
        box = QMessageBox(self)
        box.setWindowTitle("업데이트 확인")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(describe_update_status(view))
        box.exec()

    def _maybe_notify_new_mail(self, payload: object) -> None:
        if self._tray_icon is None or not isinstance(payload, dict):
            return
        if self.isActiveWindow():
            return
        message_id = payload.get("message_id")
        if message_id is None or self._api is None or self._bridge is None:
            return
        try:
            detail = self._bridge.call_backend(
                self._api.get_message_detail(message_id), timeout=3.0
            )
        except Exception:
            return
        if detail is None:
            return
        sender = detail.message.from_name or detail.message.from_addr or "(알 수 없음)"
        subject = detail.message.subject or "(제목 없음)"
        # 이 알림을 클릭했을 때 예전 승인 요청 창이 열리지 않게 한다.
        self._tray_approval_draft_id = None
        self._tray_icon.showMessage(
            "새 메일", f"{sender} — {subject}", QSystemTrayIcon.MessageIcon.Information
        )

    # ------------------------------------------------------------------
    # 종료/열 폭 저장
    # ------------------------------------------------------------------

    def _restore_column_widths(self) -> None:
        for col in range(self._message_model.columnCount()):
            width = self._qsettings.value(f"grid/col{col}", type=int)
            if width:
                self._message_grid.setColumnWidth(col, cast(int, width))

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt 오버라이드 시그니처
        for col in range(self._message_model.columnCount()):
            self._qsettings.setValue(f"grid/col{col}", self._message_grid.columnWidth(col))
        super().closeEvent(event)
