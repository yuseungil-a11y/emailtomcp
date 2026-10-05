"""앱 진입점(GUI 구동).

담당 범위: 단일 인스턴스 보장, 로깅 설정, DB 마이그레이션, Backend 시작, QSS 로드,
MainWindow 생성, 종료 시퀀스. 의존성 와이어링(서비스 조립)은 여기서 한다
(소크라테스 §1.3 "의존성 주입(와이어링)은 app.py에서 처리한다").

P1부터는 mail/storage 서비스를 실제로 조립하고, `UiApi`(이 파일 전용 퍄사드)로
감싸서 MainWindow에 주입한다. `ui` 패키지는 PySide6/`runtime.qt_bridge`/도메인
dataclass 타입 힌트만 알고, storage 연결을 직접 열거나 mail 서비스를 직접 부르지
않는다(§4.2 의존 규칙) — 모든 백엔드 호출은 이 `UiApi`의 async 메서드를
`qt_bridge.call_backend()`로 호출하는 경로 하나로 모인다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from email.utils import make_msgid
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QLockFile, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from emailtomcp._version import __version__
from emailtomcp.autoreply import cli_locator
from emailtomcp.autoreply.controller import AutoReplyController
from emailtomcp.autoreply.job_dir import cleanup_stale_job_dirs
from emailtomcp.autoreply.job_runner import JobRunner
from emailtomcp.autoreply.process_runner import SubprocessRunner
from emailtomcp.config import paths
from emailtomcp.config.settings import get_setting, set_setting
from emailtomcp.core.clock import Clock, SystemClock
from emailtomcp.core.errors import (
    AuthError,
    EmailToMcpError,
    PermanentError,
    PolicyError,
    TransientError,
)
from emailtomcp.core.events import EventBus
from emailtomcp.mail.addressing import addr_domain_norm, normalize_addr
from emailtomcp.mail.attachments_safety import (
    is_dangerous_extension,
    mark_downloaded_file,
    resolve_safe_path,
    sanitize_filename,
    unique_path,
)
from emailtomcp.mail.auth.credentials import credential_provider_for
from emailtomcp.mail.draft_service import DraftService
from emailtomcp.mail.incoming.imap import ImapIncomingProvider
from emailtomcp.mail.incoming.pop3 import Pop3IncomingProvider
from emailtomcp.mail.loop_headers import extract_loop_headers
from emailtomcp.mail.mime.parse import parse_eml
from emailtomcp.mail.outgoing import smtp as smtp_outgoing
from emailtomcp.mail.send_service import SendService
from emailtomcp.mail.sync_service import SyncService
from emailtomcp.mcp_server import local_handshake
from emailtomcp.mcp_server.approval import snapshot_hash_of
from emailtomcp.mcp_server.auth import ensure_handshake_secret
from emailtomcp.mcp_server.job_tokens import JobTokenStore
from emailtomcp.mcp_server.server import (
    EVENT_MCP_STATUS_CHANGED,
    McpComponents,
    build_mcp_components,
)
from emailtomcp.rules import engine as rule_engine
from emailtomcp.rules import output_guard
from emailtomcp.rules import schema as rule_schema
from emailtomcp.rules.autosend_verify import AutoSendVerifierImpl
from emailtomcp.runtime.asgi_host import AsgiHost, PortUnavailableError, create_loopback_socket
from emailtomcp.runtime.backend import Backend
from emailtomcp.runtime.qt_bridge import QtBridge
from emailtomcp.secrets.keyring_store import KeyringSecretStore, set_account_password
from emailtomcp.storage.db import Database
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import address_history as address_history_repo
from emailtomcp.storage.repositories import attachments as attachments_repo
from emailtomcp.storage.repositories import autoreply as autoreply_repo
from emailtomcp.storage.repositories import drafts as drafts_repo
from emailtomcp.storage.repositories import folders as folders_repo
from emailtomcp.storage.repositories import messages as messages_repo
from emailtomcp.storage.repositories import rules as rules_repo
from emailtomcp.storage.repositories.autoreply import AutoReplyRepository
from emailtomcp.ui.main_window import MainWindow
from emailtomcp.update.checker import (
    SCHEDULER_TICK_SEC,
    STARTUP_DELAY_SEC,
    UpdateChecker,
)
from emailtomcp.update.manifest import is_allowed_release_url
from emailtomcp.update.release_client import DEFAULT_BASE_URL, HttpxReleaseClient
from emailtomcp.update.state import UpdateStateStore
from emailtomcp.update.version import is_dev_build

if TYPE_CHECKING:
    from emailtomcp.mcp_server.approval import ApprovalOutcome
    from emailtomcp.mcp_server.auth import IssuedToken
    from emailtomcp.storage.repositories.accounts import Account
    from emailtomcp.storage.repositories.attachments import AttachmentMeta
    from emailtomcp.storage.repositories.autoreply import JobListRow
    from emailtomcp.storage.repositories.drafts import DraftRow
    from emailtomcp.storage.repositories.folders import Folder
    from emailtomcp.storage.repositories.mcp import McpAuditRow, McpTokenRow
    from emailtomcp.storage.repositories.messages import MessageRow
    from emailtomcp.storage.repositories.rules import RuleRow

logger = logging.getLogger(__name__)

SINGLE_INSTANCE_KEY = "EmailToMCP-single-instance"
THEME_PACKAGE = "emailtomcp.ui.resources.themes"
ICON_PACKAGE = "emailtomcp.ui.resources.icons"

# 자동 폴링/Outbox 틱 간격. 계정별 `poll_interval_sec`은 틱 안에서 "이번에 돌 때가
# 됐는지"만 판단한다(§2(a) 단순화 — 계정마다 별도 asyncio.Task를 두는 대신 하나의
# 틱 루프로 구현했다. 완료 보고에 기록).
POLL_TICK_INTERVAL_SEC = 5.0
OUTBOX_LOOP_INTERVAL_SEC = 5.0

# Outbox 상태 중 "보낼편지함" 가상 폴더에 모아서 보여줄 상태 목록(§11.1 C-19).
OUTBOX_VIRTUAL_STATUSES = ("outbox", "sending", "failed", "send_unknown")

# MCP 서버(§6.1). 포트는 설정(`mcp.port`)에 고정 저장한다. 충돌해도 자동으로 바꾸지 않는다(C-05).
DEFAULT_MCP_PORT = 8765
SETTING_MCP_PORT = "mcp.port"
# 클라이언트 유휴 판정(McpClientDisconnected)·승인 24시간 만료(C-04) 검사 주기.
MCP_HOUSEKEEPING_INTERVAL_SEC = 30.0
# 범용 UiApi.set_setting으로 쓸 수 없는 설정 키 접두사(업데이트 신뢰 상태, 보안검토 L-8).
UPDATE_SETTING_PREFIX = "update."
# 범용 UiApi.set_setting으로 쓸 수 없는 자동회신 토글·상태 키(§7.0 보호 키, I6). 보호 키 목록은
# 이 상수 한 곳에서 관리한다 — 키 문자열 자체는 storage/repositories/autoreply.py에만 둔다
# (그 밖의 곳에 키 문자열이 나오면 아키텍처 테스트가 실패한다).
AUTOREPLY_PROTECTED_SETTING_KEYS: frozenset[str] = frozenset(autoreply_repo.PROTECTED_SETTING_KEYS)
# 자동회신 설정은 `autoreply.` 접두사 전체를 범용 경로에서 기본 거부한다(보안검토 L-4,
# `update.*`와 같은 방식). 위 보호 키는 이 접두사에 포함되지만, 목록 자체는 아키텍처 테스트의
# 기준이므로 유지한다.
AUTOREPLY_SETTING_PREFIX = "autoreply."
# 예외적으로 범용 `set_setting`으로 써도 되는 `autoreply.*` 키(사용자가 UI에서 직접 정하는 값).
# Phase A에는 없다 — Phase B에서 필요한 키(예: `autoreply.timeout_sec`)가 생기면 여기에 추가한다.
# 보호 키(AUTOREPLY_PROTECTED_SETTING_KEYS)는 여기에 넣어도 거부된다.
AUTOREPLY_GENERIC_WRITABLE_KEYS: frozenset[str] = frozenset()
# claude CLI 고정값(`claude.cli_pin`, §9.3)은 사용자 확인 전용 경로(pin_claude_cli)로만 쓴다.
CLAUDE_SETTING_PREFIX = "claude."
# 자동회신 기동 복구(§7.0)는 Outbox 루프가 돌기 전에 끝나야 한다 — 그 대기 상한.
AUTOREPLY_STARTUP_TIMEOUT_SEC = 15.0


def _loop_headers_from_eml(eml_path: str) -> dict | None:
    """드라이런 전용: G1 원자료가 없는(이전 버전에서 받은) 메일의 헤더를 원문에서 다시 읽는다.

    런타임 평가에는 쓰지 않는다(원자료 없으면 평가 제외 — fail-closed). backfill 여부는 알 수
    없으므로 False로 둔다(드라이런은 LoopGuard·규칙 매칭만 보여 준다).
    """
    try:
        raw = Path(eml_path).read_bytes()
    except OSError:
        return None
    return extract_loop_headers(raw, is_backfill=False)


def _is_protected_autoreply_key(key: str) -> bool:
    """범용 경로로 쓸 수 없는 자동회신 키인지(앞뒤 공백·대소문자 차이로 우회하지 못하게 정규화).

    보호 키는 항상 거부한다. 그 밖의 `autoreply.*` 키는 화이트리스트
    (AUTOREPLY_GENERIC_WRITABLE_KEYS)에 있을 때만 허용한다(L-4, 기본 거부).
    """
    normalized = key.strip().casefold()
    if any(normalized == protected.casefold() for protected in AUTOREPLY_PROTECTED_SETTING_KEYS):
        return True
    if not normalized.startswith(AUTOREPLY_SETTING_PREFIX):
        return False
    return not any(normalized == allowed.casefold() for allowed in AUTOREPLY_GENERIC_WRITABLE_KEYS)


class _SecretMaskingFilter(logging.Filter):
    """비밀번호/토큰이 로그에 그대로 남지 않도록 막는 2차 안전망.

    1차 책임은 각 모듈이 로그 메시지를 만들 때 직접 마스킹하는 것이고, 이 필터는
    혹시 새어 들어온 `password=...`, `token=...`, `Bearer <값>` 패턴을 한 번 더 가린다.
    """

    _PATTERNS = (
        (re.compile(r"(?i)(password\s*=\s*)(\S+)"), r"\1***"),
        (re.compile(r"(?i)(token\s*=\s*)(\S+)"), r"\1***"),
        (re.compile(r"(?i)(Bearer\s+)(\S+)"), r"\1***"),
        # OAuth2(§3.x 자격증명)·MCP 토큰 변형. 토큰 자체는 `mcp_server/audit.py`의
        # `mask_args`처럼 평문을 남기지 않는 것이 1차 책임이고, 이 필터는 2차 안전망이다.
        (re.compile(r"(?i)(access_token\s*=\s*)(\S+)"), r"\1***"),
        (re.compile(r"(?i)(refresh_token\s*=\s*)(\S+)"), r"\1***"),
        (re.compile(r"(?i)(client_secret\s*=\s*)(\S+)"), r"\1***"),
        (re.compile(r"(?i)(authorization\s*:\s*)(\S+)"), r"\1***"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        masked = msg
        for pattern, repl in self._PATTERNS:
            masked = pattern.sub(repl, masked)
        if masked != msg:
            record.msg = masked
            record.args = ()
        return True


def _log_unhandled_exception(
    exc_type: type[BaseException], exc_value: BaseException, exc_tb: object
) -> None:
    """Qt 메인 스레드에서 처리되지 않은 예외를 로그에 남긴다(디버깅 로그 요구사항).

    `sys.excepthook`에 연결한다. KeyboardInterrupt는 평소대로 기본 처리기에 넘긴다.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)  # type: ignore[arg-type]
        return
    logger.critical("처리되지 않은 예외", exc_info=(exc_type, exc_value, exc_tb))  # type: ignore[arg-type]


def setup_logging() -> None:
    """stdlib logging만 쓴다. 파일 핸들러 하나를 여기서만 설정한다(소크라테스 §5.2).

    디버깅용 로그는 매일 자정 회전(`TimedRotatingFileHandler`) + 최근 7일만 보관한다
    (사용자 요구: "일주일만 저장"). 처리되지 않은 예외는 `sys.excepthook`으로 받아
    같은 로그에 남긴다.
    """
    log_path = paths.log_dir() / "emailtomcp.log"
    root_logger = logging.getLogger("emailtomcp")
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_SecretMaskingFilter())
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_SecretMaskingFilter())
    root_logger.addHandler(console_handler)

    sys.excepthook = _log_unhandled_exception


def _load_stylesheet(theme: str = "light") -> str:
    """패키지 리소스에서 QSS를 읽는다.

    importlib.resources를 써서 PyInstaller onedir 번들 안에서도 동작한다.
    """
    filename = f"{theme}.qss"
    try:
        return resources.files(THEME_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        logger.warning("QSS 테마를 찾을 수 없습니다: %s/%s", THEME_PACKAGE, filename)
        return ""


def _load_app_icon() -> QIcon:
    """앱 아이콘(유티정보 팔레트 자체 제작, 256px PNG)을 패키지 리소스에서 읽는다."""
    try:
        icon_path = resources.files(ICON_PACKAGE).joinpath("app_icon_256.png")
        with resources.as_file(icon_path) as path:
            return QIcon(str(path))
    except (FileNotFoundError, ModuleNotFoundError):
        logger.warning("앱 아이콘을 찾을 수 없습니다: %s/app_icon_256.png", ICON_PACKAGE)
        return QIcon()


def _resolve_theme(preference: str) -> str:
    """ "시스템 설정 따름"이면 `QStyleHints.colorScheme()`로 라이트/다크를 고른다(§11.0/§11.7)."""
    if preference == "system":
        scheme = QApplication.styleHints().colorScheme()
        return "dark" if scheme == Qt.ColorScheme.Dark else "light"
    return preference if preference in ("light", "dark") else "light"


def _apply_theme(app: QApplication, preference: str) -> None:
    css = _load_stylesheet(_resolve_theme(preference))
    if css:
        app.setStyleSheet(css)


# --------------------------------------------------------------------------
# UiApi — ui 패키지가 storage/mail을 직접 모르게 하는 유일한 백엔드 호출 경로.
# --------------------------------------------------------------------------


@dataclass(slots=True)
class FolderNode:
    folder: Folder
    unread_count: int


@dataclass(slots=True)
class AccountNode:
    account: Account
    folders: list[FolderNode] = field(default_factory=list)
    outbox_count: int = 0
    pending_approval_count: int = 0


@dataclass(slots=True)
class MessageDetail:
    message: MessageRow
    attachments: list[AttachmentMeta]


@dataclass(slots=True)
class ApprovalView:
    """발송 승인 다이얼로그(§11.6)에 보여 줄 내용과, 그 내용의 스냅샷 해시(H9)."""

    draft: DraftRow
    account_label: str
    snapshot_hash: str
    external_recipients: list[str]


def stdio_registration_command() -> str:
    """Claude Code stdio 프록시 등록 명령(§6.4). 토큰은 들어가지 않는다(H4)."""
    if getattr(sys, "frozen", False):
        target = f'"{sys.executable}" --mcp-stdio-proxy'
    else:
        target = f'"{sys.executable}" -m emailtomcp --mcp-stdio-proxy'
    return f"claude mcp add emailtomcp --scope user -- {target}"


class UiApi:
    """MainWindow/다이얼로그가 백엔드를 호출하는 유일한 창구.

    모든 메서드는 코루틴이다 — UI는 `bridge.call_backend(api.xxx(...))`(짧은 로컬
    DB 호출) 또는 `bridge.call_backend_async(api.xxx(...))`(네트워크 I/O처럼 오래
    걸릴 수 있는 호출, QTimer로 폴링)로만 부른다.
    """

    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        mail_blob_dir: Path,
        secret_store: KeyringSecretStore,
        sync_service: SyncService,
        send_service: SendService,
        draft_service: DraftService,
        update_checker: UpdateChecker | None = None,
        autoreply_controller: AutoReplyController | None = None,
    ) -> None:
        self._update_checker = update_checker
        self._autoreply = autoreply_controller
        self._db = db
        self._clock = clock
        self._mail_blob_dir = mail_blob_dir
        self._secret_store = secret_store
        self._sync_service = sync_service
        self._send_service = send_service
        self._draft_service = draft_service
        # MCP 구성요소는 UiApi 자신을 MailActions로 쓰므로 생성 뒤에 붙인다(attach_mcp).
        self._mcp: McpComponents | None = None
        self._mcp_runner: McpServiceRunner | None = None
        self._job_runner: JobRunner | None = None

    async def _run(self, fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003 — 내부 전용 헬퍼
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ---------------------------------------------------------------- 계정

    async def list_accounts(self) -> list[Account]:
        def _work() -> list[Account]:
            conn = self._db.new_read_connection()
            try:
                return accounts_repo.list_accounts(conn)
            finally:
                conn.close()

        return await self._run(_work)

    async def get_account(self, account_id: int) -> Account | None:
        def _work() -> Account | None:
            conn = self._db.new_read_connection()
            try:
                return accounts_repo.get_account(conn, account_id)
            finally:
                conn.close()

        return await self._run(_work)

    async def create_account(self, fields: dict, password: str | None) -> int:
        def _work() -> int:
            account_id = accounts_repo.insert_account(self._db, **fields)
            if password:
                set_account_password(self._secret_store, account_id, password)
            return account_id

        return await self._run(_work)

    async def update_account(self, account_id: int, fields: dict, password: str | None) -> None:
        def _work() -> None:
            # aliases_json은 `accounts_repo.update_account`의 허용 컬럼 목록 밖이라
            # 별도 전용 함수(`set_aliases`)로 갱신한다.
            remaining = dict(fields)
            aliases = remaining.pop("aliases", None)
            if remaining:
                accounts_repo.update_account(self._db, account_id, **remaining)
                if {"auto_reply_enabled", "enabled"} & remaining.keys():
                    self._revalidate_running_job()  # 커밋 뒤(L-4)
            if aliases is not None:
                accounts_repo.set_aliases(self._db, account_id, aliases)
            if password:
                set_account_password(self._secret_store, account_id, password)

        await self._run(_work)

    async def test_incoming_connection(self, fields: dict, password: str) -> tuple[bool, str]:
        def _work() -> tuple[bool, str]:
            provider_cls = (
                Pop3IncomingProvider
                if fields["incoming_protocol"] == "pop3"
                else ImapIncomingProvider
            )
            provider = provider_cls(
                host=fields["in_host"],
                port=fields["in_port"],
                username=fields["in_username"],
                password=password,
                security=fields["in_security"],
            )
            try:
                provider.connect()
            except EmailToMcpError as exc:
                return False, str(exc)
            else:
                return True, "수신 서버 연결에 성공했습니다."
            finally:
                try:
                    provider.close()
                except Exception:  # noqa: BLE001 — 테스트 연결 종료 실패는 결과에 영향 없음
                    logger.debug("연결 테스트 종료 중 경고", exc_info=True)

        return await self._run(_work)

    async def test_outgoing_connection(
        self, fields: dict, password: str | None
    ) -> tuple[bool, str]:
        def _work() -> tuple[bool, str]:
            try:
                smtp_outgoing.test_connection(
                    host=fields["out_host"],
                    port=fields["out_port"],
                    security=fields["out_security"],
                    username=fields.get("out_username") or fields.get("in_username"),
                    password=password,
                )
            except EmailToMcpError as exc:
                return False, str(exc)
            else:
                return True, "발신 서버 연결에 성공했습니다."

        return await self._run(_work)

    # ---------------------------------------------------------------- 트리/폴더

    async def load_tree(self) -> list[AccountNode]:
        def _work() -> list[AccountNode]:
            conn = self._db.new_read_connection()
            try:
                nodes: list[AccountNode] = []
                for account in accounts_repo.list_accounts(conn):
                    folder_nodes = [
                        FolderNode(folder=f, unread_count=messages_repo.count_unread(conn, f.id))
                        for f in folders_repo.list_folders(conn, account.id)
                    ]
                    nodes.append(
                        AccountNode(
                            account=account,
                            folders=folder_nodes,
                            outbox_count=drafts_repo.count_by_status(
                                conn, list(OUTBOX_VIRTUAL_STATUSES), account_id=account.id
                            ),
                            pending_approval_count=drafts_repo.count_by_status(
                                conn, ["pending_approval"], account_id=account.id
                            ),
                        )
                    )
                return nodes
            finally:
                conn.close()

        return await self._run(_work)

    async def fetch_now(self, account_id: int) -> int:
        return await self._run(self._sync_service.fetch_now, account_id)

    async def poll_all(self) -> dict[int, int | str]:
        return await self._run(self._sync_service.poll_all)

    # ---------------------------------------------------------------- 메시지

    async def list_messages(
        self,
        folder_id: int,
        *,
        offset: int,
        limit: int,
        sort_column: str = "received_at",
        descending: bool = True,
        unread_only: bool = False,
        flagged_only: bool = False,
        attachments_only: bool = False,
        search_text: str = "",
    ) -> tuple[list[MessageRow], int]:
        def _work() -> tuple[list[MessageRow], int]:
            conn = self._db.new_read_connection()
            try:
                return messages_repo.query_messages(
                    conn,
                    folder_id,
                    limit=limit,
                    offset=offset,
                    sort_column=sort_column,
                    descending=descending,
                    search_text=search_text,
                    unread_only=unread_only,
                    flagged_only=flagged_only,
                    attachments_only=attachments_only,
                )
            finally:
                conn.close()

        return await self._run(_work)

    async def list_outbox_drafts(self, account_id: int | None = None) -> list[DraftRow]:
        def _work() -> list[DraftRow]:
            conn = self._db.new_read_connection()
            try:
                return drafts_repo.list_by_status(
                    conn, list(OUTBOX_VIRTUAL_STATUSES), account_id=account_id
                )
            finally:
                conn.close()

        return await self._run(_work)

    async def get_message_detail(self, message_id: int) -> MessageDetail | None:
        def _work() -> MessageDetail | None:
            conn = self._db.new_read_connection()
            try:
                row = messages_repo.get_message(conn, message_id)
                if row is None:
                    return None
                return MessageDetail(
                    message=row, attachments=attachments_repo.list_attachments(conn, message_id)
                )
            finally:
                conn.close()

        return await self._run(_work)

    async def set_read(self, message_id: int, is_read: bool) -> None:
        await self._run(messages_repo.set_read, self._db, message_id, is_read)

    async def set_flagged(self, message_id: int, is_flagged: bool) -> None:
        await self._run(messages_repo.set_flagged, self._db, message_id, is_flagged)

    def _build_imap_provider(self, account: accounts_repo.Account) -> ImapIncomingProvider:
        """계정 정보로 IMAP provider를 만든다(§2(b) 서버 반영용, 테스트 연결과 동일 패턴)."""
        credential_provider = credential_provider_for(account.auth_method, self._secret_store)
        password = credential_provider.get_password(account.id)
        return ImapIncomingProvider(
            host=account.in_host,
            port=account.in_port,
            username=account.in_username,
            password=password,
            security=account.in_security,
        )

    def _reflect_imap_move(
        self,
        account: accounts_repo.Account,
        source_remote_name: str,
        remote_uid: str,
        dest_remote_name: str | None,
    ) -> None:
        """IMAP 서버에 이동을 베스트에포트로 반영한다.

        연결·이동에 실패해도(계정 오프라인, 자격증명 문제 등) 예외를 올리지
        않는다 — 호출자는 이 다음에 로컬 DB 변경을 그대로 적용해, 사용자 입장에서
        "일단 로컬에서는 옮겨진다"가 유지되게 한다(§2(b) 취지).
        """
        try:
            provider = self._build_imap_provider(account)
            provider.connect()
            try:
                dest = dest_remote_name or provider.find_role_folder("trash")
                if dest is None:
                    logger.warning(
                        "IMAP 서버에서 대상 폴더를 찾지 못해 로컬만 반영합니다(계정 %s)",
                        account.id,
                    )
                    return
                # move()는 COPYUID를 거의 항상 받지 못하는 라이브러리 제약이 있다
                # (mail/incoming/imap.py 모듈 docstring 참조) — 반환값은 쓰지 않고,
                # 로컬 remote_uid는 다음 동기화 때 메시지ID/크기로 재매칭되는 기존
                # 경로에 맡긴다(new_remote_uid=None).
                provider.move(source_remote_name, remote_uid, dest)
            finally:
                provider.close()
        except (TransientError, EmailToMcpError) as exc:
            logger.warning("IMAP 서버 반영에 실패했습니다(로컬 변경은 유지): %s", exc)

    async def move_to_trash(self, message_id: int) -> None:
        """휴지통 폴더로 옮긴다. IMAP 계정이면 서버에도 이동을 반영한다(§2(b), §12.3 ⑯).

        POP3는 설계대로 로컬 전용이다. 서버 반영은 베스트에포트다(`_reflect_imap_move`).
        """

        def _work() -> None:
            conn = self._db.new_read_connection()
            try:
                row = messages_repo.get_message(conn, message_id)
                account = accounts_repo.get_account(conn, row.account_id) if row else None
                source_folder = folders_repo.get_folder(conn, row.folder_id) if row else None
                trash = folders_repo.find_by_role(conn, row.account_id, "trash") if row else None
            finally:
                conn.close()
            if row is None or account is None:
                return
            if trash is None:
                trash = folders_repo.get_or_create_folder(
                    self._db, row.account_id, "휴지통", role="trash"
                )

            if (
                account.incoming_protocol == "imap"
                and row.remote_uid is not None
                and source_folder is not None
                and source_folder.remote_name
            ):
                self._reflect_imap_move(
                    account, source_folder.remote_name, row.remote_uid, trash.remote_name
                )

            messages_repo.move_to_folder(
                self._db,
                message_id,
                new_folder_id=trash.id,
                new_remote_uid=None,
                keep_original_folder=True,
            )

        await self._run(_work)

    async def restore_from_trash(self, message_id: int) -> None:
        """휴지통에서 원래 폴더로 복구한다. IMAP이면 서버에도 베스트에포트로 반영한다."""

        def _work() -> None:
            conn = self._db.new_read_connection()
            try:
                row = messages_repo.get_message(conn, message_id)
                has_original = row is not None and row.original_folder_id is not None
                account = accounts_repo.get_account(conn, row.account_id) if has_original else None
                source_folder = folders_repo.get_folder(conn, row.folder_id) if row else None
                dest_folder = (
                    folders_repo.get_folder(conn, row.original_folder_id) if has_original else None
                )
            finally:
                conn.close()
            if row is None or row.original_folder_id is None:
                return

            if (
                account is not None
                and account.incoming_protocol == "imap"
                and row.remote_uid is not None
                and source_folder is not None
                and source_folder.remote_name
                and dest_folder is not None
                and dest_folder.remote_name
            ):
                self._reflect_imap_move(
                    account, source_folder.remote_name, row.remote_uid, dest_folder.remote_name
                )

            messages_repo.move_to_folder(
                self._db,
                message_id,
                new_folder_id=row.original_folder_id,
                new_remote_uid=None,
                keep_original_folder=False,
            )

        await self._run(_work)

    async def get_raw_eml(self, message_id: int) -> bytes:
        def _work() -> bytes:
            conn = self._db.new_read_connection()
            try:
                row = messages_repo.get_message(conn, message_id)
            finally:
                conn.close()
            if row is None:
                raise PermanentError(f"메일을 찾을 수 없습니다: {message_id}")
            return Path(row.eml_path).read_bytes()

        return await self._run(_work)

    async def save_attachment(self, message_id: int, part_index: str, dest_dir: str) -> str:
        def _work() -> str:
            conn = self._db.new_read_connection()
            try:
                row = messages_repo.get_message(conn, message_id)
            finally:
                conn.close()
            if row is None:
                raise PermanentError(f"메일을 찾을 수 없습니다: {message_id}")
            parsed = parse_eml(Path(row.eml_path).read_bytes())
            match = next((a for a in parsed.attachments if a.part_index == part_index), None)
            if match is None:
                raise PermanentError(f"첨부를 찾을 수 없습니다: {part_index}")
            filename = sanitize_filename(match.filename_raw) if match.filename_raw else "attachment"
            dest_path = unique_path(resolve_safe_path(Path(dest_dir), filename))
            dest_path.write_bytes(match.payload)
            mark_downloaded_file(dest_path)
            return str(dest_path)

        return await self._run(_work)

    async def is_attachment_dangerous(self, filename: str) -> bool:
        return is_dangerous_extension(filename)

    # ---------------------------------------------------------------- 작성/발송

    async def new_draft(
        self,
        account_id: int,
        *,
        to_addrs: list[str],
        cc_addrs: list[str],
        bcc_addrs: list[str],
        subject: str | None,
        body_text: str | None,
    ) -> int:
        return await self._run(
            self._draft_service.create_draft,
            account_id=account_id,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            bcc_addrs=bcc_addrs,
            subject=subject,
            body_text=body_text,
        )

    async def reply_draft(self, account_id: int, source_message_id: int, reply_all: bool) -> int:
        return await self._run(
            self._draft_service.create_reply_draft,
            account_id=account_id,
            source_message_id=source_message_id,
            body_text="",
            reply_all=reply_all,
        )

    async def forward_draft(self, account_id: int, source_message_id: int, mode: str) -> int:
        return await self._run(
            self._draft_service.create_forward_draft,
            account_id=account_id,
            source_message_id=source_message_id,
            to_addrs=[],
            mode=mode,
        )

    async def get_draft(self, draft_id: int) -> DraftRow | None:
        def _work() -> DraftRow | None:
            conn = self._db.new_read_connection()
            try:
                return drafts_repo.get_draft(conn, draft_id)
            finally:
                conn.close()

        return await self._run(_work)

    async def update_compose(
        self,
        draft_id: int,
        *,
        to_addrs: list[str],
        cc_addrs: list[str],
        bcc_addrs: list[str],
        subject: str | None,
        body_text: str | None,
        attachments: list[dict],
    ) -> bool:
        return await self._run(
            self._draft_service.update_compose,
            draft_id,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            bcc_addrs=bcc_addrs,
            subject=subject,
            body_text=body_text,
            attachments=attachments,
        )

    async def discard_draft(self, draft_id: int) -> bool:
        return await self._run(self._draft_service.discard_draft, draft_id)

    async def stage_local_attachment(self, file_path: str) -> dict:
        return await self._run(self._draft_service.stage_local_attachment, file_path)

    async def send_draft_now(
        self,
        draft_id: int,
        *,
        to_addrs: list[str],
        cc_addrs: list[str],
        bcc_addrs: list[str],
        subject: str | None,
        body_text: str | None,
        attachments: list[dict],
    ) -> str:
        """작성 창 [보내기]. 작성 창이 **화면에 보여 준 내용**을 필수 인자로 받는다.

        그 내용의 저장과 `draft → outbox` 전이를 writer 작업 하나로 처리한다(보안검토
        N-1/R-1) — 저장과 발송 사이에 MCP `update_draft`가 끼어들어 사용자가 보지 못한
        수신자가 승인 없이 나가는 경로를 없앤다. 내용 인자를 생략하고 DB에 있는 값을 그대로
        보내는 경로는 일부러 두지 않는다(fail-closed).
        """

        def _work() -> str:
            # 작성 창 [보내기]는 `draft`에서만 Outbox로 보낸다 — 승인 대기 중인 초안은
            # 스냅샷 해시를 검증하는 승인 경로로만 나갈 수 있다(H9).
            ok = self._draft_service.save_compose_and_mark_outbox(
                draft_id,
                to_addrs=to_addrs,
                cc_addrs=cc_addrs,
                bcc_addrs=bcc_addrs,
                subject=subject,
                body_text=body_text,
                attachments=attachments,
                message_id_hdr=make_msgid(),
                approved_by="user_send",
            )
            if not ok:
                return "already_processed"
            return self._send_service.send_now(draft_id)

        return await self._run(_work)

    async def retry_draft(self, draft_id: int) -> str:
        """ "보낼편지함"의 [재시도] — failed/send_unknown을 outbox로 돌리고 즉시 재발송한다."""

        def _work() -> str:
            ok = drafts_repo.transition_status(
                self._db, draft_id, ["failed", "send_unknown"], "outbox"
            )
            if not ok:
                return "not_retryable"
            return self._send_service.send_now(draft_id)

        return await self._run(_work)

    async def delete_outbox_draft(self, draft_id: int) -> bool:
        return await self._run(self._draft_service.discard_draft, draft_id)

    async def list_known_addresses(self, account_id: int) -> list[str]:
        def _work() -> list[str]:
            conn = self._db.new_read_connection()
            try:
                return address_history_repo.list_known_addresses(conn, account_id)
            finally:
                conn.close()

        return await self._run(_work)

    # ---------------------------------------------------------------- 설정

    async def get_setting(self, key: str, default: object = None) -> object:
        def _work() -> object:
            conn = self._db.new_read_connection()
            try:
                return get_setting(conn, key, default)
            finally:
                conn.close()

        return await self._run(_work)

    async def set_setting(self, key: str, value: object) -> None:
        # 업데이트 신뢰 상태(update.last_issued_at 등)는 update/state.py 경유로만 쓴다.
        # 범용 경로로 미래 시각을 써 넣으면 업데이트 확인을 영구 동결할 수 있다(보안검토 L-8).
        if key.startswith(UPDATE_SETTING_PREFIX):
            raise PolicyError(f"업데이트 상태 설정({key})은 이 경로로 바꿀 수 없습니다")
        # 자동회신 토글·세대·큐 상태는 AutoReplyController 전용 경로로만 바꾼다(§7.0 I6).
        if _is_protected_autoreply_key(key):
            raise PolicyError(f"자동회신 상태 설정({key})은 이 경로로 바꿀 수 없습니다")
        if key.strip().casefold().startswith(CLAUDE_SETTING_PREFIX):
            raise PolicyError(f"claude CLI 설정({key})은 이 경로로 바꿀 수 없습니다")
        await self._run(set_setting, self._db, key, value)

    async def log_dir_path(self) -> str:
        """디버깅 로그 폴더 경로(도움말 > 로그 폴더 열기). `ui`가 `config.paths`를
        직접 import하지 않도록(§4.2) 이 창구로만 노출한다."""
        return await self._run(lambda: str(paths.log_dir()))

    # ---------------------------------------------------------------- 자동회신 토글(§7.0, §11.7)

    def _require_autoreply(self) -> AutoReplyController:
        if self._autoreply is None:
            raise PermanentError("자동회신 컨트롤러가 준비되지 않았습니다")
        return self._autoreply

    async def get_autoreply_toggle(self) -> dict:
        """설정 > 자동회신 탭·상태줄 표시용 스냅샷과 켜기 사전점검(§7.0 켜기 1단계).

        `generation`은 켜기 CAS의 `expected_generation`으로 그대로 돌려보낸다. 사전점검 중
        claude CLI 고정 여부와 unmatched_action 적용은 Phase B(cli_locator·RuleEngine) 이후
        항목이라 지금은 "해당없음"(None)으로 둔다.
        """

        def _work() -> dict:
            conn = self._db.new_read_connection()
            try:
                state = autoreply_repo.get_toggle_state(conn)
                counts = autoreply_repo.preflight_counts(conn)
                unmatched = get_setting(conn, "autoreply.unmatched_action", "ignore")
                pin = cli_locator.pin_from_setting(
                    get_setting(conn, cli_locator.SETTING_CLI_PIN, None)
                )
            finally:
                conn.close()
            return {
                "enabled": state.enabled,
                "generation": state.generation,
                "changed_at": state.enabled_changed_at,
                "watermark_message_id": state.watermark_message_id,
                "preflight": {
                    **counts,
                    "unmatched_action": unmatched if unmatched in ("ignore", "draft") else "ignore",
                    # 미고정이면 고정 템플릿 규칙만 동작한다(§7.0 켜기 1단계).
                    "claude_cli_pinned": pin is not None,
                },
            }

        return await self._run(_work)

    async def disable_autoreply(self) -> dict:
        """끄기 — 확인창 없이 바로 적용(§11.7). CAS 없음, 항상 성공(L-A)."""
        result = await self._require_autoreply().disable()
        drain = result.drain
        return {
            "enabled": False,
            "generation": result.generation,
            "cancelled_jobs": drain.cancelled_jobs if drain else 0,
            "reverted_outbox": drain.reverted_outbox if drain else 0,
            "sending_in_flight": drain.sending_in_flight if drain else 0,
        }

    async def enable_autoreply(self, expected_generation: int) -> dict:
        """켜기 — compare-and-set. 세대가 다르면 PolicyError(UI가 경고 후 다시 읽는다)."""
        result = await self._require_autoreply().enable(expected_generation=expected_generation)
        return {"enabled": True, "generation": result.generation}

    # ---------------------------------------------------------------- 자동회신 규칙(§11.4, P3 B)

    @staticmethod
    def _rule_view(row: RuleRow) -> dict:
        try:
            conditions = json.loads(row.conditions_json)
        except (TypeError, ValueError):
            conditions = {"match": "all", "conditions": []}
        return {
            "id": row.id,
            "name": row.name,
            "account_id": row.account_id,
            "enabled": row.enabled,
            "priority": row.priority,
            "conditions": conditions,
            "action": row.action,
            "reply_mode": row.reply_mode,
            "fixed_template": row.fixed_template,
            "extra_instructions": row.extra_instructions,
            "max_chars": row.max_chars,
            "allow_thread_context": row.allow_thread_context,
            "accept_aligned_dkim": row.accept_aligned_dkim,
            "cooldown_hours": row.cooldown_hours,
            "version": row.version,
        }

    async def list_rules(self) -> list[dict]:
        def _work() -> list[dict]:
            conn = self._db.new_read_connection()
            try:
                return [self._rule_view(r) for r in rules_repo.list_rules(conn)]
            finally:
                conn.close()

        return await self._run(_work)

    async def get_rule(self, rule_id: int) -> dict | None:
        def _work() -> dict | None:
            conn = self._db.new_read_connection()
            try:
                row = rules_repo.get_rule(conn, rule_id)
            finally:
                conn.close()
            return self._rule_view(row) if row else None

        return await self._run(_work)

    async def save_rule(
        self, rule_id: int | None, data: dict, expected_version: int | None = None
    ) -> dict:
        """규칙 저장(G0). 검증 실패는 PolicyError(사용자에게 그대로 보이는 한국어 메시지).

        Phase B: action은 ignore/draft만 저장할 수 있다(auto_send는 다음 업데이트).
        """

        def _work() -> dict:
            try:
                validated = rule_schema.validate_rule(data, allow_auto_send=False)
            except rule_schema.RuleSchemaError as exc:
                raise PolicyError(str(exc)) from exc
            if rule_id is None:
                new_id = rules_repo.insert_rule(
                    self._db, fields=validated.fields, enabled=validated.enabled
                )
                return {"id": new_id, "version": 1, "warnings": validated.warnings}
            if expected_version is None:
                raise PolicyError("규칙 버전 정보가 없습니다. 다시 열어 주세요")
            version = rules_repo.update_rule(
                self._db, rule_id, expected_version=expected_version, fields=validated.fields
            )
            rules_repo.set_rule_enabled(self._db, rule_id, validated.enabled)
            self._revalidate_running_job()  # 커밋 뒤(L-4)
            return {"id": rule_id, "version": version, "warnings": validated.warnings}

        return await self._run(_work)

    async def delete_rule(self, rule_id: int) -> bool:
        def _work() -> bool:
            deleted = rules_repo.delete_rule(self._db, rule_id)
            self._revalidate_running_job()  # 커밋 뒤(L-4)
            return deleted

        return await self._run(_work)

    async def set_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        def _work() -> bool:
            changed = rules_repo.set_rule_enabled(self._db, rule_id, bool(enabled))
            if not enabled:
                self._revalidate_running_job()  # 커밋 뒤(L-4)
            return changed

        return await self._run(_work)

    def _revalidate_running_job(self) -> None:
        """규칙·계정 변경 커밋 뒤, 실행 중 잡이 더는 유효하지 않으면 러너가 토큰 폐기 → kill 한다
        (데카르트 31번 L-4·Spinoza 30번 L-4). 실패해도 G7 gate가 결과를 막으므로 로그만 남긴다."""
        runner = self._job_runner
        if runner is None:
            return
        try:
            runner.revalidate_current()
        except Exception:  # noqa: BLE001
            logger.warning("실행 중 자동회신 잡 재확인 실패", exc_info=True)

    async def reorder_rules(self, ordered_ids: list[int]) -> None:
        await self._run(rules_repo.set_priorities, self._db, [int(i) for i in ordered_ids])

    async def dry_run_rule(self, data: dict, limit: int = 50) -> dict:
        """드라이런(§11.4): claude 실행·DB 쓰기 없이 최근 메일에 규칙을 적용해 본다.

        저장 전 규칙(편집 중 값)도 그대로 쓴다. 토글 off에서도 동작한다(§7.0 적용 범위).
        """

        def _work() -> dict:
            try:
                conditions = rule_schema.parse_conditions(
                    data.get("conditions", {"match": "all", "conditions": []})
                )
            except rule_schema.RuleSchemaError as exc:
                raise PolicyError(str(exc)) from exc
            account_id = data.get("account_id")
            conn = self._db.new_read_connection()
            try:
                report = rule_engine.dry_run_rule(
                    conn,
                    conditions=conditions,
                    action=str(data.get("action") or "draft"),
                    account_id=account_id if isinstance(account_id, int) else None,
                    normalize=normalize_addr,
                    headers_fallback=_loop_headers_from_eml,
                    limit=max(1, min(int(limit), 200)),
                )
            finally:
                conn.close()
            return {
                "total": report.total,
                "matched": report.matched,
                "blocked": report.blocked,
                "would_draft": report.would_draft,
                "rows": [
                    {
                        "message_id": r.message_id,
                        "received_at": r.received_at,
                        "from_addr": r.from_addr,
                        "subject": r.subject,
                        "matched": r.matched,
                        "loop_reasons": list(r.loop_reasons),
                        "result": r.result,
                        "note": r.note,
                    }
                    for r in report.rows
                ],
            }

        return await self._run(_work)

    async def get_unmatched_action(self) -> str:
        def _work() -> str:
            conn = self._db.new_read_connection()
            try:
                return rule_engine.read_unmatched_action(conn)
            finally:
                conn.close()

        return await self._run(_work)

    async def set_unmatched_action(self, value: str) -> None:
        """규칙 미매칭 시 동작(§7.2 4단계): ignore(기본) 또는 draft. 범용 경로는 막혀 있다."""
        if value not in ("ignore", "draft"):
            raise PolicyError("미매칭 동작은 '무시' 또는 '초안'만 고를 수 있습니다")
        await self._run(set_setting, self._db, rule_engine.SETTING_UNMATCHED_ACTION, value)

    # ---------------------------------------------------------------- 자동회신 잡(§11.5, P3 B)

    async def list_autoreply_jobs(self, limit: int = 100) -> list[JobListRow]:
        def _work() -> list[JobListRow]:
            conn = self._db.new_read_connection()
            try:
                return autoreply_repo.list_recent_jobs(conn, limit=max(1, min(int(limit), 500)))
            finally:
                conn.close()

        return await self._run(_work)

    async def get_draft_guard_findings(self, draft_id: int) -> list[str] | None:
        """자동회신 초안의 출력 가드 결과(작성 창 경고 배너, §7.5). 자동회신 초안이 아니면 None."""

        def _work() -> list[str] | None:
            conn = self._db.new_read_connection()
            try:
                found = autoreply_repo.get_draft_guard_findings(conn, int(draft_id))
            finally:
                conn.close()
            return None if found is None else list(found)

        return await self._run(_work)

    async def get_autoreply_draft_source(self, draft_id: int) -> str | None:
        """자동회신 초안 생성 방식(claude/fixed_template/unknown), 아니면 None(작성 창, I-4)."""

        def _work() -> str | None:
            conn = self._db.new_read_connection()
            try:
                return autoreply_repo.get_autoreply_draft_source(conn, int(draft_id))
            finally:
                conn.close()

        return await self._run(_work)

    async def get_autoreply_queue_status(self) -> dict:
        def _work() -> dict:
            conn = self._db.new_read_connection()
            try:
                toggle = autoreply_repo.get_toggle_state(conn)
                pin = cli_locator.pin_from_setting(
                    get_setting(conn, cli_locator.SETTING_CLI_PIN, None)
                )
                return {
                    "enabled": toggle.enabled,
                    "queue_state": autoreply_repo.get_queue_state(conn),
                    "autosend_state": autoreply_repo.get_autosend_state(conn),
                    "counts": autoreply_repo.count_jobs_by_status(conn),
                    "cli": {
                        "pinned": pin is not None,
                        "program": str(pin.program) if pin else None,
                        "script": str(pin.script) if pin and pin.script else None,
                        "problem": cli_locator.verify_pin(pin),
                    },
                }
            finally:
                conn.close()

        return await self._run(_work)

    async def set_autoreply_queue_paused(self, paused: bool) -> str:
        """[일시정지]/[재개](§11.5). 전역 토글이 꺼져 있으면 거부한다."""

        def _work() -> str:
            conn = self._db.new_read_connection()
            try:
                enabled = autoreply_repo.get_toggle_state(conn).enabled
            finally:
                conn.close()
            if not enabled:
                raise PolicyError("자동회신이 꺼져 있습니다")
            return autoreply_repo.set_queue_paused(self._db, paused=bool(paused))

        state = await self._run(_work)
        if not paused and self._job_runner is not None:
            self._job_runner.wake()
        return state

    async def discover_claude_cli(self) -> list[dict]:
        """claude 후보 경로(실행하지 않음, §9.3)."""

        def _work() -> list[dict]:
            return [
                {"program": c.program, "script": c.script, "source": c.source}
                for c in cli_locator.discover_candidates()
            ]

        return await self._run(_work)

    async def pin_claude_cli(self, program: str, script: str | None) -> dict:
        """사용자가 확인한 claude 경로를 절대경로+sha256으로 고정한다(§9.3 4번)."""

        def _work() -> dict:
            try:
                value = cli_locator.make_pin(program, script, now=self._clock.now())
            except ValueError as exc:
                raise PolicyError(str(exc)) from exc
            set_setting(self._db, cli_locator.SETTING_CLI_PIN, value)
            return {"program": value["program"], "script": value["script"]}

        return await self._run(_work)

    def attach_job_runner(self, runner: JobRunner) -> None:
        self._job_runner = runner

    # ---------------------------------------------------------------- 업데이트(§11.7, §11.8)

    def _update_view(self, status: dict) -> dict:
        """UI에 넘기기 전에 링크를 한 번 더 검증하고, 표시용 부가 정보를 붙인다."""
        view = dict(status)
        if not is_allowed_release_url(view.get("release_page")):
            view["release_page"] = None
        checker = self._update_checker
        view["auto_check"] = checker.auto_check_enabled() if checker else False
        view["stale"] = checker.is_stale() if checker else False
        return view

    async def get_update_status(self) -> dict:
        """마지막 확인 결과(저장된 스냅샷). 확인을 새로 하지는 않는다."""
        checker = self._update_checker
        if checker is None:
            return {"kind": "unverifiable", "reason": "업데이트 확인을 사용할 수 없습니다"}
        return await self._run(lambda: self._update_view(checker.last_status().to_dict()))

    async def check_update_now(self) -> dict:
        """설정 화면 [지금 확인] — 수동 확인 1회(경보 정지 상태여도 수행)."""
        checker = self._update_checker
        if checker is None:
            return {"kind": "unverifiable", "reason": "업데이트 확인을 사용할 수 없습니다"}
        return await self._run(lambda: self._update_view(checker.check_once(manual=True).to_dict()))

    async def set_update_auto_check(self, enabled: bool) -> None:
        if self._update_checker is not None:
            await self._run(self._update_checker.set_auto_check, enabled)

    # ---------------------------------------------------------------- MCP(§6, §11.5, §11.6)

    def attach_mcp(self, components: McpComponents, runner: McpServiceRunner) -> None:
        self._mcp = components
        self._mcp_runner = runner

    def _require_mcp(self) -> McpComponents:
        if self._mcp is None:
            raise PermanentError("MCP 서버 구성요소가 준비되지 않았습니다")
        return self._mcp

    async def mcp_status(self) -> dict:
        runner = self._mcp_runner
        status = (
            runner.snapshot()
            if runner is not None
            else {"running": False, "port": None, "error": "MCP가 구성되지 않았습니다"}
        )
        status["client_connected"] = self._mcp.tracker.connected if self._mcp else False
        status["stdio_command"] = stdio_registration_command()
        return status

    async def list_mcp_tokens(self) -> list[McpTokenRow]:
        return await self._run(self._require_mcp().tokens.list_tokens)

    async def issue_mcp_token(
        self, *, kind: str, scopes: list[str], name: str, ttl_days: int = 180
    ) -> IssuedToken:
        tokens = self._require_mcp().tokens
        if kind == "proxy":
            return await self._run(
                tokens.issue, kind=kind, scopes=scopes, client=name or "default", ttl_days=ttl_days
            )
        return await self._run(
            tokens.issue, kind=kind, scopes=scopes, label=name, ttl_days=ttl_days
        )

    async def revoke_mcp_token(self, token_id: int) -> bool:
        return await self._run(self._require_mcp().tokens.revoke, token_id)

    async def list_mcp_audit(self, limit: int = 200) -> list[McpAuditRow]:
        return await self._run(self._require_mcp().audit.list_recent, limit)

    async def list_pending_approvals(self) -> list[DraftRow]:
        def _work() -> list[DraftRow]:
            conn = self._db.new_read_connection()
            try:
                return drafts_repo.list_by_status(conn, ["pending_approval"])
            finally:
                conn.close()

        return await self._run(_work)

    async def get_approval_view(self, draft_id: int) -> ApprovalView | None:
        """승인 다이얼로그용 — 승인 대기 상태가 아니면 None."""

        def _work() -> ApprovalView | None:
            conn = self._db.new_read_connection()
            try:
                draft = drafts_repo.get_draft(conn, draft_id)
                account = accounts_repo.get_account(conn, draft.account_id) if draft else None
            finally:
                conn.close()
            if draft is None or account is None or draft.status != "pending_approval":
                return None
            own_domains = {
                addr_domain_norm(account.email_address),
                *(addr_domain_norm(a) for a in account.aliases),
            } - {""}
            recipients = [*draft.to_addrs, *draft.cc_addrs, *draft.bcc_addrs]
            return ApprovalView(
                draft=draft,
                account_label=f"{account.display_name} <{account.email_address}>",
                snapshot_hash=snapshot_hash_of(draft),
                external_recipients=[
                    a for a in recipients if addr_domain_norm(a) not in own_domains
                ],
            )

        return await self._run(_work)

    async def approve_draft(self, draft_id: int, expected_hash: str) -> ApprovalOutcome:
        return await self._run(self._require_mcp().approval.approve, draft_id, expected_hash)

    async def reject_draft(self, draft_id: int) -> bool:
        return await self._run(self._require_mcp().approval.reject, draft_id)

    async def cancel_approval_for_edit(self, draft_id: int) -> bool:
        return await self._run(self._require_mcp().approval.cancel_for_edit, draft_id)


# --------------------------------------------------------------------------
# 백그라운드 스케줄러(수신 폴링/Outbox) — Backend의 asyncio 루프 위에서 돈다.
# --------------------------------------------------------------------------


def _poll_tick_sync(
    sync_service: SyncService, db: Database, due: dict[int, float], clock: Clock
) -> None:
    conn = db.new_read_connection()
    try:
        accounts = accounts_repo.list_enabled_accounts(conn)
    finally:
        conn.close()
    now = clock.monotonic()
    for account in accounts:
        last = due.get(account.id, 0.0)
        if now - last < account.poll_interval_sec:
            continue
        due[account.id] = now
        try:
            sync_service.fetch_now(account.id)
        except Exception:  # noqa: BLE001 — 계정 하나의 실패가 틱 전체를 막으면 안 됨(S-15)
            logger.warning("계정 %s 자동 폴링 실패", account.id, exc_info=True)


async def _poll_loop(
    sync_service: SyncService, db: Database, clock: Clock, stop_event: asyncio.Event
) -> None:
    due: dict[int, float] = {}
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        try:
            await loop.run_in_executor(None, _poll_tick_sync, sync_service, db, due, clock)
        except Exception:  # noqa: BLE001 — 틱 하나의 실패가 루프 자체를 죽이면 안 됨
            logger.exception("수신 폴링 틱 처리 중 예상치 못한 오류")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_TICK_INTERVAL_SEC)


async def _outbox_loop(send_service: SendService, stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        try:
            await loop.run_in_executor(None, send_service.process_outbox)
        except Exception:  # noqa: BLE001 — 틱 하나의 실패가 루프 자체를 죽이면 안 됨
            logger.exception("Outbox 처리 틱 중 예상치 못한 오류")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=OUTBOX_LOOP_INTERVAL_SEC)


def _schedule_background_loops(
    backend: Backend,
    sync_service: SyncService,
    send_service: SendService,
    db: Database,
    clock: Clock,
) -> tuple[asyncio.Event, list[asyncio.Task]]:
    """Backend 루프 스레드 안에서 폴링/Outbox 태스크를 만든다(스레드 간 안전하게).

    반환한 태스크 목록은 종료 시퀀스에서 `stop_event.set()` 뒤에 명시적으로
    취소한다 — 그렇게 하지 않으면 `backend.stop()`이 루프를 바로 멈추면서
    "Task was destroyed but it is pending" 경고가 남는다(동작에는 영향 없지만
    로그가 지저분해진다).
    """
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []

    def _create_tasks() -> None:
        loop = backend.loop
        tasks.append(loop.create_task(_poll_loop(sync_service, db, clock, stop_event)))
        tasks.append(loop.create_task(_outbox_loop(send_service, stop_event)))

    backend.loop.call_soon_threadsafe(_create_tasks)
    return stop_event, tasks


def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()


# 업데이트 매니페스트 base_url 덮어쓰기 환경변수 — dev 빌드에서만 받는다(§4.6).
UPDATE_BASE_URL_ENV = "EMAILTOMCP_UPDATE_BASE_URL"


def _resolve_update_base_url(version: str) -> str:
    override = os.environ.get(UPDATE_BASE_URL_ENV, "").strip()
    if not override:
        return DEFAULT_BASE_URL
    if not is_dev_build(version):
        logger.warning("정식 빌드에서는 %s를 무시합니다(dev 빌드 전용)", UPDATE_BASE_URL_ENV)
        return DEFAULT_BASE_URL
    logger.info("업데이트 base_url 덮어쓰기(dev 빌드): %s", override)
    return override


def _build_update_checker(
    db: Database, event_bus: EventBus, clock: Clock, version: str
) -> UpdateChecker:
    """업데이트 확인기 조립(§3.4, §14.4). 서명 검증 키는 update.keys의 내장 상수를 쓴다."""
    return UpdateChecker(
        http_client=HttpxReleaseClient(
            _resolve_update_base_url(version), user_agent_version=version
        ),
        state_store=UpdateStateStore(db),
        event_bus=event_bus,
        clock=clock,
        current_version=version,
    )


def _schedule_update_checks(backend: Backend, checker: UpdateChecker) -> None:
    """기동 30초 뒤 첫 확인, 이후 1시간마다 '24시간 지났는지' 보고 확인한다(§3.4)."""
    first_run = {"pending": True}

    def _tick() -> None:
        force = first_run["pending"]
        first_run["pending"] = False
        checker.run_scheduled(force=force)

    backend.schedule_periodic(
        _tick, initial_delay=STARTUP_DELAY_SEC, interval=SCHEDULER_TICK_SEC, name="update-check"
    )


# --------------------------------------------------------------------------
# MCP 서버 수명 관리(§6.1, §2(a) 종료 순서 4번)
# --------------------------------------------------------------------------


def _resolve_mcp_port(cli_port: int | None, configured: object) -> int:
    """`--mcp-port`(우선) 또는 설정 `mcp.port`. 0(임의 포트)은 dev 빌드에서만 허용한다(§4.6)."""
    if cli_port is not None:
        if cli_port == 0 and not is_dev_build(__version__):
            logger.warning("--mcp-port 0은 dev·테스트 빌드에서만 허용됩니다. 설정값을 씁니다")
        elif 0 <= cli_port <= 65535:
            return cli_port
    try:
        port = int(configured)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return DEFAULT_MCP_PORT
    return port if 1024 <= port <= 65535 else DEFAULT_MCP_PORT


class McpServiceRunner:
    """MCP HTTP 서버를 Backend 루프 위에서 켜고 끈다(app.py 조립 전용).

    - 소켓은 `runtime.asgi_host.create_loopback_socket`이 만든다(127.0.0.1 전용, Windows
      `SO_EXCLUSIVEADDRUSE`). 포트를 쓸 수 없으면 **자동으로 바꾸지 않고** 꺼진 상태로
      두고 `McpStatusChanged(error=...)`로 알린다(C-05).
    - 세션 매니저(`session_manager.run()`)는 호스트 lifespan으로 직접 연다(D-06).
    """

    def __init__(self, components: McpComponents, event_bus: EventBus, *, port: int) -> None:
        self._components = components
        self._event_bus = event_bus
        self._configured_port = port
        self._host: AsgiHost | None = None
        self._lock = threading.Lock()
        self._state: dict = {"running": False, "port": port, "error": None}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def current_port(self) -> int | None:
        """로컬 핸드셰이크 응답용. 실행 중이 아니면 None(=MCP 비활성 응답).

        상태 플래그만 믿지 않고 **호스트가 실제로 서비스 중인지**(uvicorn 태스크가 살아
        있는지)까지 확인한다(보안검토 M-3). 이미 닫힌 포트를 알려 주면, 그 포트를 먼저 잡은
        다른 로컬 프로세스에 프록시가 토큰을 보낼 수 있기 때문이다.
        """
        with self._lock:
            host = self._host
            if not self._state["running"] or host is None or not host.running:
                return None
            return int(self._state["port"])

    def _set_state(self, *, running: bool, port: int, error: str | None) -> None:
        with self._lock:
            self._state = {"running": running, "port": port, "error": error}
            payload = dict(self._state)
        self._event_bus.publish(EVENT_MCP_STATUS_CHANGED, payload)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        with contextlib.suppress(Exception):
            await loop.run_in_executor(None, self._components.audit.purge_expired)
        try:
            sock = create_loopback_socket(self._configured_port)
        except PortUnavailableError as exc:
            logger.warning("MCP 서버를 시작하지 못했습니다(포트 자동 변경 없음): %s", exc)
            self._set_state(
                running=False,
                port=self._configured_port,
                error=(
                    f"포트 {self._configured_port}를 사용할 수 없습니다"
                    "(다른 프로그램이 사용 중일 수 있음)"
                ),
            )
            return
        port = int(sock.getsockname()[1])
        host = AsgiHost(
            self._components.build_asgi(port),
            sock,
            lifespan=self._components.tool_server.session_manager.run,
            # host는 호출 시점(서비스 종료 시)에 이미 할당되어 있다.
            on_exit=lambda: self._on_host_exit(host),
        )
        try:
            await host.start()
        except Exception as exc:  # noqa: BLE001 — 시작 실패는 상태로 알리고 앱은 계속 동작
            logger.exception("MCP HTTP 서버 시작 실패")
            self._set_state(running=False, port=port, error=str(exc))
            return
        if not host.running:
            # start()가 돌아온 직후 이미 죽었다면 "실행 중"으로 알리지 않는다(M-3).
            self._set_state(running=False, port=port, error="MCP 서버가 시작 직후 종료되었습니다")
            return
        with self._lock:
            self._host = host
        logger.info("MCP 서버 시작: 127.0.0.1:%s", port)
        self._set_state(running=True, port=port, error=None)

    def _on_host_exit(self, host: AsgiHost) -> None:
        """호스트 서비스 종료 콜백(Backend 루프). 지금 관리 중인 호스트가 예기치 않게
        죽은 경우에만 상태를 즉시 내리고 알린다 — stop()으로 끈 경우는 stop()이 이미
        상태를 내렸으므로 아무것도 하지 않는다(M-3)."""
        with self._lock:
            if self._host is not host:
                return
            self._host = None
        logger.warning("MCP HTTP 서버가 예기치 않게 종료되었습니다: 127.0.0.1:%s", host.port)
        self._set_state(
            running=False, port=host.port, error="MCP 서버가 예기치 않게 종료되었습니다"
        )

    async def stop(self) -> None:
        # 소켓을 닫기 **전에** 먼저 "실행 중 아님"으로 내린다(M-3) — 종료가 진행되는 동안
        # 핸드셰이크가 곧 닫힐 포트를 알려 주지 않게 한다.
        with self._lock:
            host, self._host = self._host, None
        if host is not None:
            self._set_state(running=False, port=host.port, error=None)
            await host.stop()
            logger.info("MCP 서버 종료: 127.0.0.1:%s", host.port)

    def housekeeping(self) -> None:
        """주기 작업(블로킹): 클라이언트 유휴 판정, 24시간 지난 승인 대기 만료(C-04)."""
        self._components.tracker.check_idle()
        self._components.approval.expire_due()


class SingleInstanceGuard:
    """QLockFile + QLocalServer로 단일 인스턴스를 보장한다(DESIGN.md §2.a).

    두 번째 실행은 `try_acquire`가 False를 반환하기 전에, 이미 떠 있는 인스턴스로
    활성화 요청(`activate`)을 보낸다.

    로컬 서버가 받는 명령은 **`activate`와 `mcp_handshake` 두 가지뿐**이다(L2, §6.4).
    해석은 Qt를 모르는 `mcp_server.local_handshake.parse_command`가 하고, 그 밖의 입력은
    응답 없이 연결을 끊는다.
    """

    _IDLE_TIMEOUT_MS = 3000

    def __init__(self, key: str) -> None:
        self._key = key
        self._lock_file = QLockFile(str(paths.lock_file_path()))
        self._server: QLocalServer | None = None
        self._on_activate = None
        self._on_handshake = None
        self._clients: set[QLocalSocket] = set()

    def try_acquire(self, on_activate_requested, on_handshake=None) -> bool:  # noqa: ANN001 — Qt 슬롯 콜백
        if self._lock_file.tryLock(100):
            QLocalServer.removeServer(self._key)
            self._server = QLocalServer()
            self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            self._server.listen(self._key)
            self._on_activate = on_activate_requested
            self._on_handshake = on_handshake
            self._server.newConnection.connect(self._on_new_connection)
            return True

        # 이미 떠 있는 인스턴스에 활성화 요청을 보낸다.
        socket = QLocalSocket()
        socket.connectToServer(self._key)
        if socket.waitForConnected(200):
            socket.write(b"activate\n")
            socket.flush()
            socket.waitForBytesWritten(200)
            socket.disconnectFromServer()
        return False

    def _on_new_connection(self) -> None:
        server = self._server
        while server is not None and server.hasPendingConnections():
            sock = server.nextPendingConnection()
            if sock is None:
                break
            state = {"buf": b"", "done": False}
            self._clients.add(sock)
            sock.readyRead.connect(lambda s=sock, st=state: self._on_ready_read(s, st))
            sock.disconnected.connect(lambda s=sock, st=state: self._finish(s, st, closing=True))
            QTimer.singleShot(
                self._IDLE_TIMEOUT_MS, lambda s=sock, st=state: self._on_idle_timeout(s, st)
            )
            if sock.bytesAvailable():
                self._on_ready_read(sock, state)

    def _on_ready_read(self, sock: QLocalSocket, state: dict) -> None:
        state["buf"] += bytes(sock.readAll().data())
        if b"\n" in state["buf"] or len(state["buf"]) >= local_handshake.MAX_COMMAND_BYTES:
            self._finish(sock, state, closing=False)

    def _on_idle_timeout(self, sock: QLocalSocket, state: dict) -> None:
        if not state["done"] and sock in self._clients:
            state["done"] = True
            sock.abort()

    def _finish(self, sock: QLocalSocket, state: dict, *, closing: bool) -> None:
        if state["done"]:
            if closing:
                self._clients.discard(sock)
                sock.deleteLater()
            return
        state["done"] = True
        command, argument = local_handshake.parse_command(state["buf"].split(b"\n", 1)[0])
        if command == local_handshake.CMD_ACTIVATE and self._on_activate is not None:
            self._on_activate()
        elif (
            command == local_handshake.CMD_HANDSHAKE
            and argument is not None
            and self._on_handshake is not None
            and not closing
        ):
            response = self._on_handshake(argument)
            if response:
                sock.write(response)
                sock.flush()
        elif command == local_handshake.CMD_INVALID:
            logger.warning("허용되지 않은 로컬 서버 명령을 거부했습니다")
        if closing:
            self._clients.discard(sock)
            sock.deleteLater()
        else:
            sock.disconnectFromServer()

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        self._lock_file.unlock()


def run_app(mcp_port: int | None = None) -> int:
    """GUI 애플리케이션을 구동한다. `mcp_port`(`--mcp-port`)가 없으면 설정 `mcp.port`를 쓴다."""
    setup_logging()
    logger.info("EmailToMCP %s 시작", __version__)

    app = QApplication(sys.argv)
    app.setApplicationName("EmailToMCP")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("UTInfo")
    app_icon = _load_app_icon()
    app.setWindowIcon(app_icon)

    guard = SingleInstanceGuard(SINGLE_INSTANCE_KEY)
    window_holder: dict[str, MainWindow] = {}
    # 로컬 핸드셰이크(§6.4)는 아래에서 공유비밀과 MCP 러너가 준비된 뒤에 채운다.
    handshake_holder: dict[str, object] = {}

    def _activate_existing(*_args: object) -> None:
        window = window_holder.get("window")
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()

    def _on_handshake(nonce_hex: str) -> bytes | None:
        secret = handshake_holder.get("secret")
        runner = handshake_holder.get("runner")
        if not isinstance(secret, bytes) or not isinstance(runner, McpServiceRunner):
            return None
        return local_handshake.build_response(secret, nonce_hex, runner.current_port())

    if not guard.try_acquire(_activate_existing, _on_handshake):
        logger.info("이미 실행 중인 인스턴스가 있어 활성화 요청만 보내고 종료합니다")
        return 0

    db = Database(paths.db_path())
    db.start()
    applied_version = db.migrate()
    logger.info("DB 마이그레이션 완료: user_version=%s", applied_version)

    conn = db.new_read_connection()
    try:
        theme_pref = get_setting(conn, "theme", "system")
        configured_mcp_port = get_setting(conn, SETTING_MCP_PORT, DEFAULT_MCP_PORT)
    finally:
        conn.close()
    _apply_theme(app, theme_pref)

    event_bus = EventBus()
    clock = SystemClock()
    secret_store = KeyringSecretStore()
    mail_blob_dir = paths.mail_blob_dir()

    def credential_provider_factory(auth_method: str):  # noqa: ANN001, ANN202
        return credential_provider_for(auth_method, secret_store)

    sync_service = SyncService(
        db,
        mail_blob_dir=mail_blob_dir,
        credential_provider_factory=credential_provider_factory,
        event_bus=event_bus,
        clock=clock,
    )
    send_service = SendService(
        db,
        mail_blob_dir=mail_blob_dir,
        credential_provider_factory=credential_provider_factory,
        clock=clock,
        event_bus=event_bus,
    )
    draft_service = DraftService(db, mail_blob_dir=mail_blob_dir)
    send_service.recover_on_startup()
    update_checker = _build_update_checker(db, event_bus, clock, __version__)

    # ---- 자동회신(P3 Phase B, §7.0·§7.9): 판정기는 여기서 한 번만 주입한다(§7.11 M-D).
    job_tokens = JobTokenStore()
    autoreply_repository = AutoReplyRepository(
        db, verifier=AutoSendVerifierImpl(now_func=clock.now)
    )
    mcp_runner_holder: dict[str, McpServiceRunner] = {}

    def _current_mcp_port() -> int | None:
        holder_runner = mcp_runner_holder.get("runner")
        return holder_runner.current_port() if holder_runner is not None else None

    jobs_base_dir = paths.jobs_dir()
    try:
        cleanup_stale_job_dirs(jobs_base_dir)
    except Exception:  # noqa: BLE001 — 정리 실패가 기동을 막으면 안 된다
        logger.warning("남은 잡 디렉터리 정리 실패", exc_info=True)
    job_runner = JobRunner(
        db,
        clock=clock,
        repo=autoreply_repository,
        token_issuer=job_tokens,
        process_runner=SubprocessRunner(),
        jobs_base_dir=jobs_base_dir,
        mcp_port=_current_mcp_port,
        guard_check=output_guard.check,
        event_bus=event_bus,
    )
    autoreply_engine = rule_engine.RuleEngine(
        db,
        clock=clock,
        event_bus=event_bus,
        normalize_addr=normalize_addr,
        on_job_created=job_runner.wake,
    )
    autoreply_controller = AutoReplyController(
        db, clock=clock, event_bus=event_bus, engine=autoreply_engine, runner=job_runner
    )
    # MCP 서버가 (다시) 켜지면 대기 중인 잡을 처리하도록 러너를 깨운다.
    event_bus.subscribe(EVENT_MCP_STATUS_CHANGED, lambda _n, _p: job_runner.wake())

    ui_api = UiApi(
        db=db,
        clock=clock,
        mail_blob_dir=mail_blob_dir,
        secret_store=secret_store,
        sync_service=sync_service,
        send_service=send_service,
        draft_service=draft_service,
        update_checker=update_checker,
        autoreply_controller=autoreply_controller,
    )
    ui_api.attach_job_runner(job_runner)

    # ---- MCP 서버(§6): 구성요소 조립. 메일 서버 반영이 섞인 동작은 UiApi 경로를 재사용한다.
    def _read_setting(key: str, default: object) -> object:
        read_conn = db.new_read_connection()
        try:
            return get_setting(read_conn, key, default)
        finally:
            read_conn.close()

    def _send_allowed_by_floor() -> bool:
        """min_version_floor 미달(업데이트 확인 결과 `update.state.below_floor`)이면 False."""
        state = _read_setting("update.state", None)
        return not (isinstance(state, dict) and state.get("below_floor"))

    mcp_components = build_mcp_components(
        db=db,
        clock=clock,
        event_bus=event_bus,
        secret_store=secret_store,
        draft_service=draft_service,
        mail_actions=ui_api,
        get_setting=_read_setting,
        send_allowed_by_floor=_send_allowed_by_floor,
        version=__version__,
        job_tokens=job_tokens,
        job_submission_sink=job_runner,
    )
    mcp_runner = McpServiceRunner(
        mcp_components, event_bus, port=_resolve_mcp_port(mcp_port, configured_mcp_port)
    )
    mcp_runner_holder["runner"] = mcp_runner
    ui_api.attach_mcp(mcp_components, mcp_runner)
    try:
        handshake_holder["secret"] = ensure_handshake_secret(secret_store)
    except AuthError:
        logger.warning("OS keyring을 쓸 수 없어 stdio 프록시 핸드셰이크를 비활성화합니다")
    handshake_holder["runner"] = mcp_runner

    backend = Backend()
    backend.start()
    # 자동회신 기동 복구(§7.0 "앱 기동 시"). off로 저장돼 있으면 남은 auto_reply 잡을 cancelled,
    # policy_auto_send outbox를 draft로 돌린다 — Outbox 루프가 그 outbox를 먼저 집어 가지 않도록
    # 백그라운드 루프를 예약하기 **전에** 끝낸다. 실패하면 컨트롤러는 OFF로 남는다(fail-closed).
    try:
        backend.submit(
            autoreply_controller.start_if_enabled(), timeout=AUTOREPLY_STARTUP_TIMEOUT_SEC
        )
    except Exception:  # noqa: BLE001 — 자동회신 복구 실패가 메일 클라이언트 기동을 막으면 안 된다
        logger.exception("자동회신 기동 복구 중 예외 — 자동회신은 꺼진 상태로 시작합니다")
    bridge = QtBridge(backend, event_bus)
    stop_event, background_tasks = _schedule_background_loops(
        backend, sync_service, send_service, db, clock
    )
    _schedule_update_checks(backend, update_checker)

    window = MainWindow(
        version=__version__,
        event_bus=event_bus,
        bridge=bridge,
        ui_api=ui_api,
        apply_theme=lambda pref: _apply_theme(app, pref),
    )
    window.setWindowIcon(app_icon)
    window_holder["window"] = window
    window.show()

    # MCP 서버는 창이 이벤트를 받을 준비가 된 뒤 시작한다(McpStatusChanged를 놓치지 않게).
    try:
        backend.submit(mcp_runner.start(), timeout=15.0)
    except Exception:  # noqa: BLE001 — MCP 실패가 메일 클라이언트 기동을 막으면 안 된다
        logger.exception("MCP 서버 시작 중 예외")
    backend.schedule_periodic(
        mcp_runner.housekeeping,
        initial_delay=MCP_HOUSEKEEPING_INTERVAL_SEC,
        interval=MCP_HOUSEKEEPING_INTERVAL_SEC,
        name="mcp-housekeeping",
    )

    def _shutdown() -> None:
        logger.info("종료 시퀀스 시작")
        # 1. 새 작업 수락 중단(폴링/Outbox 루프에 정지 신호 + 태스크 명시적 취소)
        backend.call_soon_threadsafe(stop_event.set)
        backend.call_soon_threadsafe(lambda: _cancel_tasks(background_tasks))
        # (§2(a) 4번) uvicorn과 MCP 세션 매니저 종료 — 대기 중인 send_draft 호출도 여기서 끝난다.
        try:
            backend.submit(mcp_runner.stop(), timeout=15.0)
        except Exception:  # noqa: BLE001 — 종료는 계속 진행한다
            logger.warning("MCP 서버 종료 중 오류", exc_info=True)
        # 자동회신 컨트롤러 정리(저장된 토글 값은 그대로 둔다)
        try:
            backend.submit(autoreply_controller.shutdown(), timeout=5.0)
        except Exception:  # noqa: BLE001 — 종료는 계속 진행한다
            logger.warning("자동회신 컨트롤러 종료 중 오류", exc_info=True)
        # 루프 stop, 스레드 join, executor 종료 (Backend.stop 내부에서 순서대로 처리)
        backend.stop()
        # 5. writer 종료
        db.stop()
        # 6. 락 해제
        guard.release()
        logger.info("종료 시퀀스 완료")

    app.aboutToQuit.connect(_shutdown)

    return app.exec()
