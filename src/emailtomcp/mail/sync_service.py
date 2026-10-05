"""수신 동기화 서비스 (DESIGN.md §2(b), §4.5 S-15 계정 장애 격리).

계정별로 폴링하고, 신규 메일을 parse → limits 검사 → DB 저장(단일 writer 경유) →
`core.events`로 `MessageReceived` 이벤트 발행까지 담당한다. 계정 하나의 예외가
다른 계정 폴링을 막지 않는다(`poll_all`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from emailtomcp.core.errors import EmailToMcpError, PermanentError
from emailtomcp.mail.addressing import normalize_addr
from emailtomcp.mail.attachments_safety import is_dangerous_extension, sanitize_filename
from emailtomcp.mail.auth.credentials import CredentialProvider
from emailtomcp.mail.incoming.base import FetchedMessage, IncomingProvider
from emailtomcp.mail.incoming.imap import ImapIncomingProvider
from emailtomcp.mail.incoming.pop3 import Pop3IncomingProvider
from emailtomcp.mail.loop_headers import dumps_loop_headers, extract_loop_headers
from emailtomcp.mail.mime.parse import parse_eml
from emailtomcp.mail.models import ParsedMessage
from emailtomcp.mail.threading import compute_thread_key
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import folders as folders_repo
from emailtomcp.storage.repositories import messages as messages_repo
from emailtomcp.storage.repositories import pop3_uidl as pop3_uidl_repo
from emailtomcp.storage.repositories.attachments import AttachmentInsert
from emailtomcp.storage.repositories.messages import MessageInsert

if TYPE_CHECKING:
    from emailtomcp.core.clock import Clock
    from emailtomcp.core.events import EventBus
    from emailtomcp.storage.db import Database
    from emailtomcp.storage.repositories.accounts import Account

logger = logging.getLogger(__name__)

CredentialProviderFactory = Callable[[str], CredentialProvider]

EVENT_MESSAGE_RECEIVED = "MessageReceived"
# 메일 서버 연결상태 아이콘(폴더 트리/상태줄, 2026-10-04 신규, DESIGN.md §4.2·§11.1).
EVENT_ACCOUNT_CONNECTED = "AccountConnected"
EVENT_ACCOUNT_ERROR = "AccountError"


def _flag_present(flags: frozenset[str], name: str) -> bool:
    return any(f.lstrip("\\").lower() == name.lower() for f in flags)


class SyncService:
    """계정별 폴링 + 수신 저장 (운영 구현은 poplib/imapclient, DI는 `CredentialProvider`)."""

    def __init__(
        self,
        db: Database,
        *,
        mail_blob_dir: Path,
        credential_provider_factory: CredentialProviderFactory,
        event_bus: EventBus,
        clock: Clock,
    ) -> None:
        self._db = db
        self._mail_blob_dir = mail_blob_dir
        self._credential_provider_factory = credential_provider_factory
        self._event_bus = event_bus
        self._clock = clock
        # 최초 동기화 목록에 있었지만 그때 저장에 실패한 UID(폴더별, 이 프로세스 수명 동안만).
        # 완료 표식은 이미 찍혔어도 이 UID를 다시 받을 때는 backfill로 본다(데카르트 33번 M-5).
        self._pending_backfill_uids: dict[int, set[str]] = {}

    # --- 공개 API ------------------------------------------------------

    def fetch_now(self, account_id: int) -> int:
        """수동 즉시수신. 새로 저장한 메시지 개수를 돌려준다."""
        try:
            saved = self._poll_account(account_id)
        except Exception as exc:  # noqa: BLE001 — 계정 연결상태 이벤트로 알리고 다시 던진다
            self._publish_account_error(account_id, str(exc))
            raise
        else:
            self._publish_account_connected(account_id)
            return saved

    def poll_all(self) -> dict[int, int | str]:
        """등록된 모든 활성 계정을 폴링한다. 계정 단위로 예외를 격리한다(S-15)."""
        conn = self._db.new_read_connection()
        try:
            accounts = accounts_repo.list_enabled_accounts(conn)
        finally:
            conn.close()

        results: dict[int, int | str] = {}
        for account in accounts:
            try:
                results[account.id] = self._poll_account(account.id)
            except Exception as exc:  # noqa: BLE001 — 계정 하나의 실패가 다른 계정을 막으면 안 됨(S-15)
                logger.exception("계정 %s 수신 처리 실패", account.id)
                results[account.id] = str(exc)
                self._publish_account_error(account.id, str(exc))
            else:
                self._publish_account_connected(account.id)
        return results

    # --- 연결상태 이벤트 ---------------------------------------------------

    def _publish_account_connected(self, account_id: int) -> None:
        logger.debug("계정 %s 연결 상태: 정상", account_id)
        self._event_bus.publish(
            EVENT_ACCOUNT_CONNECTED, {"account_id": account_id, "at": self._clock.now().isoformat()}
        )

    def _publish_account_error(self, account_id: int, error: str) -> None:
        logger.warning("계정 %s 연결 오류: %s", account_id, error)
        self._event_bus.publish(
            EVENT_ACCOUNT_ERROR,
            {"account_id": account_id, "error": error, "at": self._clock.now().isoformat()},
        )

    # --- 내부 구현 ------------------------------------------------------

    def _build_provider(self, account: Account) -> IncomingProvider:
        credential_provider = self._credential_provider_factory(account.auth_method)
        password = credential_provider.get_password(account.id)
        if account.incoming_protocol == "pop3":
            return Pop3IncomingProvider(
                host=account.in_host,
                port=account.in_port,
                username=account.in_username,
                password=password,
                security=account.in_security,
            )
        if account.incoming_protocol == "imap":
            return ImapIncomingProvider(
                host=account.in_host,
                port=account.in_port,
                username=account.in_username,
                password=password,
                security=account.in_security,
            )
        raise PermanentError(f"알 수 없는 수신 프로토콜입니다: {account.incoming_protocol}")

    def _poll_account(self, account_id: int) -> int:
        conn = self._db.new_read_connection()
        try:
            account = accounts_repo.get_account(conn, account_id)
        finally:
            conn.close()
        if account is None:
            raise PermanentError(f"계정을 찾을 수 없습니다: {account_id}")
        if not account.enabled:
            return 0

        logger.info(
            "계정 %s 수신 동기화 시작(%s)", account_id, account.incoming_protocol
        )
        provider = self._build_provider(account)
        provider.connect()
        try:
            inbox_folder = folders_repo.get_or_create_folder(
                self._db, account.id, "INBOX", role="inbox"
            )
            if account.incoming_protocol == "imap":
                self._sync_imap_folder_list(account.id, provider)
            saved = self._poll_folder(account, provider, inbox_folder.id, "INBOX")
        finally:
            provider.close()
        logger.info("계정 %s 수신 동기화 완료: %s건 저장", account_id, saved)
        return saved

    def _sync_imap_folder_list(self, account_id: int, provider: IncomingProvider) -> None:
        """IMAP 서버 폴더 목록을 로컬 `folders` 테이블에 동기화한다(§2(b))."""
        remote_folders = provider.list_folders()
        for remote_folder in remote_folders:
            folders_repo.get_or_create_folder(
                self._db,
                account_id,
                remote_folder.name,
                role=remote_folder.role,
                remote_name=remote_folder.name,
            )

    def _poll_folder(
        self, account: Account, provider: IncomingProvider, folder_id: int, remote_folder_name: str
    ) -> int:
        conn = self._db.new_read_connection()
        try:
            known_uids = messages_repo.get_known_remote_uids(conn, account.id, folder_id)
            if account.incoming_protocol == "pop3":
                known_uids |= pop3_uidl_repo.get_seen_uidls(conn, account.id)
            folder = folders_repo.get_folder(conn, folder_id)
        finally:
            conn.close()

        # 이 폴더의 최초 동기화를 아직 마치지 않았으면 이번에 받는 메일은 과거 메일 일괄
        # 수신(backfill)이다 — 자동회신 평가에서 제외한다(§7.2 1단계, 과거 메일 폭주 방지).
        # 판정은 폴더별 영속 표식(folders.initial_sync_done, m0004)으로 하고 로컬 메일 수와는
        # 무관하다 — 받은편지함을 비운 뒤 받는 새 메일이 backfill로 빠지지 않는다
        # (데카르트 31번 M-1).
        # 한계: 빈 메일함 계정의 "첫 동기화" 때 받은 메일은 backfill이다(안전한 방향).
        # UIDVALIDITY 변경 재동기화 감지는 IMAP UID 상태 추적이 생길 때 함께 넣는다.
        is_initial_sync = folder is None or not folder.initial_sync_done
        pending = self._pending_backfill_uids.get(folder_id, set())
        summaries = provider.list_new(remote_folder_name, known_uids=known_uids)
        saved = 0
        failed_uids: set[str] = set()
        for summary in summaries:
            uid = summary.remote_uid
            # backfill 판정은 "이 최초 동기화 시점에 보려던 UID 목록"에 들었는지로 한다.
            is_backfill = is_initial_sync or uid in pending
            try:
                self._fetch_and_store(
                    account,
                    provider,
                    folder_id,
                    remote_folder_name,
                    uid,
                    is_backfill=is_backfill,
                )
                saved += 1
                pending.discard(uid)
            except EmailToMcpError:
                raise
            except Exception:  # noqa: BLE001 — 메일 한 통 실패가 나머지를 막으면 안 됨
                logger.exception(
                    "메일 저장 실패: account=%s folder=%s uid=%s",
                    account.id,
                    remote_folder_name,
                    uid,
                )
                if is_initial_sync:
                    failed_uids.add(uid)

        # 최초 동기화 목록을 끝까지 훑었으면 일부 메일이 실패했어도 완료로 기록한다(데카르트
        # 33번 M-5: 계속 실패하는 메일 한 통 때문에 표식이 영영 안 찍혀, 이후 새 메일이 전부
        # backfill로 빠지던 문제). 실패한 메일은 이 프로세스가 살아 있는 동안 다시 받을 때도
        # backfill로 본다. 앱을 다시 시작한 뒤 받으면 일반 메일로 평가되지만, 72시간 경과 메일은
        # 평가 단계에서 제외되고 Phase B 결과물은 초안뿐이라 영향이 작다(계속 실패하는 메일 하나가
        # 폴더 전체의 자동회신을 막는 것보다 안전한 쪽을 택함).
        # 목록 조회·연결 오류(EmailToMcpError)로 중간에 끊기면 위에서 예외가 올라가 표식을
        # 찍지 않는다 — 다음 동기화가 남은 목록을 다시 backfill로 받는다.
        if is_initial_sync and folder is not None:
            folders_repo.mark_initial_sync_done(self._db, folder_id)
            if failed_uids:
                logger.warning(
                    "최초 동기화 중 %s건 저장 실패(완료 표식은 기록): account=%s folder=%s",
                    len(failed_uids),
                    account.id,
                    remote_folder_name,
                )
                pending = pending | failed_uids
        if pending:
            self._pending_backfill_uids[folder_id] = pending
        else:
            self._pending_backfill_uids.pop(folder_id, None)

        if account.incoming_protocol == "pop3":
            self._apply_pop3_retention(account, provider, remote_folder_name)
        return saved

    def _fetch_and_store(
        self,
        account: Account,
        provider: IncomingProvider,
        folder_id: int,
        remote_folder_name: str,
        remote_uid: str,
        *,
        is_backfill: bool = False,
    ) -> None:
        fetched: FetchedMessage = provider.fetch(remote_folder_name, remote_uid)
        parsed = parse_eml(fetched.raw)
        if parsed.parse_limited:
            logger.warning(
                "MIME 상한 초과/파싱 실패로 파싱 제한 모드로 저장합니다: account=%s uid=%s",
                account.id,
                remote_uid,
            )

        if not is_backfill and self._autoreply_handled_copy_exists(account.id, parsed.message_id):
            # 같은 Message-ID 사본이 이미 자동회신 평가를 거쳤으면(또는 backfill로 빠졌으면) 이
            # 사본은 평가 대상에서 뺀다 — 서버 이동 실패 상태에서 로컬만 휴지통으로 옮긴 메일이
            # INBOX에서 다시 내려올 때 잡이 또 생기지 않게 한다(데카르트 33번 L-9). 잡 유니크
            # 인덱스는 로컬 messages.id 기준이라 새 행으로 들어온 재수신 사본을 막지 못한다.
            logger.info(
                "이미 자동회신 처리된 메일의 재수신 사본 — 평가에서 제외: account=%s uid=%s",
                account.id,
                remote_uid,
            )
            is_backfill = True

        eml_path = self._write_eml(fetched.raw)
        is_read = _flag_present(fetched.flags, "Seen")
        is_flagged = _flag_present(fetched.flags, "Flagged")
        received_at = self._clock.now()

        data = self.to_message_insert(
            account_id=account.id,
            folder_id=folder_id,
            parsed=parsed,
            remote_uid=remote_uid,
            eml_path=str(eml_path),
            received_at=received_at,
            is_read=is_read,
            is_flagged=is_flagged,
            auto_headers_json=dumps_loop_headers(
                extract_loop_headers(fetched.raw, is_backfill=is_backfill)
            ),
        )
        message_id = messages_repo.insert_message(self._db, data)

        if account.incoming_protocol == "pop3":
            pop3_uidl_repo.record_seen(self._db, account.id, remote_uid, received_at.isoformat())
            if not account.pop3_leave_on_server:
                try:
                    provider.delete(remote_folder_name, remote_uid)
                except EmailToMcpError:
                    logger.warning(
                        "POP3 서버 삭제 실패(leave_on_server=False): uid=%s",
                        remote_uid,
                        exc_info=True,
                    )

        self._event_bus.publish(
            EVENT_MESSAGE_RECEIVED,
            {
                "message_id": message_id,
                "account_id": account.id,
                "folder_id": folder_id,
                "parse_limited": parsed.parse_limited,
                "is_backfill": is_backfill,
            },
        )

    def _autoreply_handled_copy_exists(self, account_id: int, message_id_hdr: str | None) -> bool:
        if not message_id_hdr:
            return False
        conn = self._db.new_read_connection()
        try:
            return messages_repo.has_autoreply_handled_copy(conn, account_id, message_id_hdr)
        finally:
            conn.close()

    def _apply_pop3_retention(
        self, account: Account, provider: IncomingProvider, remote_folder_name: str
    ) -> None:
        """`pop3_delete_after_days` 정책(서버 보관 N일 후 삭제)을 적용한다(§2(b))."""
        if not account.pop3_leave_on_server or account.pop3_delete_after_days is None:
            return
        conn = self._db.new_read_connection()
        try:
            seen = pop3_uidl_repo.list_seen(conn, account.id)
        finally:
            conn.close()

        now = self._clock.now()
        for item in seen:
            try:
                first_seen = datetime.fromisoformat(item.first_seen_at)
            except ValueError:
                continue
            age_days = (now - first_seen).days
            if age_days < account.pop3_delete_after_days:
                continue
            try:
                provider.delete(remote_folder_name, item.uidl)
            except EmailToMcpError:
                # 이미 서버에서 지워졌거나(다른 클라이언트), 이번 세션 UIDL 목록에 없는 경우.
                logger.debug("POP3 보관기간 만료 삭제 실패(무시): uidl=%s", item.uidl)

    def _write_eml(self, raw: bytes) -> Path:
        filename = f"{uuid.uuid4()}.eml"
        path = self._mail_blob_dir / filename
        path.write_bytes(raw)
        return path

    @staticmethod
    def to_message_insert(
        *,
        account_id: int,
        folder_id: int,
        parsed: ParsedMessage,
        remote_uid: str | None,
        eml_path: str,
        received_at: datetime,
        is_read: bool,
        is_flagged: bool,
        auto_headers_json: str | None = None,
    ) -> MessageInsert:
        from_addr = parsed.from_addrs[0].addr if parsed.from_addrs else None
        from_name = parsed.from_addrs[0].name if parsed.from_addrs else None
        from_addr_norm = normalize_addr(from_addr) if from_addr else None
        sender_addr = parsed.sender_addr.addr if parsed.sender_addr else None

        attachments_insert: list[AttachmentInsert] = []
        for att in parsed.attachments:
            filename_safe = sanitize_filename(att.filename_raw) if att.filename_raw else None
            dangerous = is_dangerous_extension(filename_safe) if filename_safe else False
            sha256 = hashlib.sha256(att.payload).hexdigest() if att.payload else None
            attachments_insert.append(
                AttachmentInsert(
                    part_index=att.part_index,
                    filename_raw=att.filename_raw,
                    filename_safe=filename_safe,
                    content_type=att.content_type,
                    size_bytes=att.size_bytes,
                    sha256=sha256,
                    content_id=att.content_id,
                    is_inline=att.is_inline,
                    is_dangerous=dangerous,
                )
            )

        has_attachments = any(not a.is_inline for a in parsed.attachments)
        snippet = (parsed.body_text or "").strip().replace("\n", " ")[:200]
        thread_key = compute_thread_key(account_id, parsed)

        return MessageInsert(
            account_id=account_id,
            folder_id=folder_id,
            message_id_hdr=parsed.message_id,
            in_reply_to=parsed.in_reply_to,
            references_hdr=" ".join(parsed.references) if parsed.references else None,
            thread_key=thread_key,
            remote_uid=remote_uid,
            from_addr=from_addr,
            from_name=from_name,
            from_count=len(parsed.from_addrs),
            sender_addr=sender_addr,
            to_addrs_json=json.dumps([a.addr for a in parsed.to_addrs], ensure_ascii=False),
            cc_addrs_json=json.dumps([a.addr for a in parsed.cc_addrs], ensure_ascii=False),
            bcc_addrs_json=json.dumps([a.addr for a in parsed.bcc_addrs], ensure_ascii=False),
            reply_to_json=json.dumps([a.addr for a in parsed.reply_to_addrs], ensure_ascii=False),
            from_addr_norm=from_addr_norm,
            from_plain=from_addr or "",
            to_plain=", ".join(a.addr for a in parsed.to_addrs),
            cc_plain=", ".join(a.addr for a in parsed.cc_addrs),
            subject=parsed.subject,
            snippet=snippet,
            body_text=parsed.body_text,
            body_source=parsed.body_source,
            content_mismatch=parsed.content_mismatch,
            has_html=parsed.has_html,
            date_hdr=parsed.date.isoformat() if parsed.date else None,
            received_at=received_at.isoformat(),
            size_bytes=parsed.size_bytes,
            is_read=is_read,
            is_flagged=is_flagged,
            has_attachments=has_attachments,
            eml_path=eml_path,
            parse_limited=parsed.parse_limited,
            priority=parsed.priority,
            attachments=attachments_insert,
            auto_headers_json=auto_headers_json,
        )
