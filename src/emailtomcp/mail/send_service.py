"""Outbox 발송 서비스 (DESIGN.md §5.3 상태 전이표, §2(b) Sent APPEND, §4.4 재시도 분류).

`draft(outbox)` → SMTP 발송 → `sent` 전이를 담당한다. 재시도는 `TransientError`일
때만 백오프(1분/5분/15분) 최대 3회이고, `PermanentError`/`AuthError`는 즉시 `failed`다.
기동 시 `sending`으로 남아있던 초안은 `send_unknown`으로 두고 **자동 재발송하지
않는다**(§5.3).

최종 발신 MIME은 여기서(발송 시점에) 조립한다 — `drafts` 테이블의 to/cc/bcc/제목/
본문은 사용자가 편집했을 수 있는 "현재 값"이므로, `draft_service`가 만들어 둔
`attachments_json`(메타데이터만)을 이 시점에 실제 바이트로 해석(resolve)한다.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from emailtomcp.core.errors import AuthError, EmailToMcpError, PermanentError, TransientError
from emailtomcp.mail.auth.credentials import CredentialProvider
from emailtomcp.mail.incoming.imap import ImapIncomingProvider
from emailtomcp.mail.mime import build as mime_build
from emailtomcp.mail.mime.build import OutgoingAttachment
from emailtomcp.mail.mime.parse import parse_eml
from emailtomcp.mail.outgoing import smtp as smtp_outgoing
from emailtomcp.mail.sync_service import EVENT_ACCOUNT_CONNECTED, EVENT_ACCOUNT_ERROR, SyncService
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import drafts as drafts_repo
from emailtomcp.storage.repositories import folders as folders_repo
from emailtomcp.storage.repositories import messages as messages_repo

if TYPE_CHECKING:
    from emailtomcp.core.clock import Clock
    from emailtomcp.core.events import EventBus
    from emailtomcp.storage.db import Database
    from emailtomcp.storage.repositories.accounts import Account
    from emailtomcp.storage.repositories.drafts import DraftRow

logger = logging.getLogger(__name__)

CredentialProviderFactory = Callable[[str], CredentialProvider]

MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))

_REPLY_KINDS = ("reply", "reply_all")

# 발송 완료/실패(§4.2 이벤트 목록). 계정 연결상태는 sync_service와 같은 이름을 쓴다.
EVENT_SEND_COMPLETED = "SendCompleted"
EVENT_SEND_FAILED = "SendFailed"


class SendService:
    """Outbox 패턴 발송기(DESIGN.md §5.3)."""

    def __init__(
        self,
        db: Database,
        *,
        mail_blob_dir: Path,
        credential_provider_factory: CredentialProviderFactory,
        clock: Clock,
        event_bus: EventBus | None = None,
    ) -> None:
        self._db = db
        self._mail_blob_dir = mail_blob_dir
        self._credential_provider_factory = credential_provider_factory
        self._clock = clock
        self._event_bus = event_bus

    def _publish(self, name: str, payload: dict) -> None:
        if self._event_bus is not None:
            self._event_bus.publish(name, payload)

    # --- 공개 API --------------------------------------------------------

    def recover_on_startup(self) -> None:
        """기동 시 `sending`이던 초안을 `send_unknown`으로 둔다(자동 재발송 금지, §5.3)."""
        conn = self._db.new_read_connection()
        try:
            stale = drafts_repo.list_sending_stale(conn)
        finally:
            conn.close()
        for draft in stale:
            drafts_repo.mark_send_unknown(self._db, draft.id)

    def process_outbox(self) -> dict[int, str]:
        """`status='outbox'`이고 발송 시각이 된 초안을 모두 처리한다."""
        now_iso = self._clock.now().isoformat()
        conn = self._db.new_read_connection()
        try:
            ready = drafts_repo.list_outbox_ready(conn, now_iso=now_iso)
        finally:
            conn.close()

        results: dict[int, str] = {}
        for draft in ready:
            try:
                results[draft.id] = self._send_one(draft)
            except Exception as exc:  # noqa: BLE001 — 초안 하나의 실패가 나머지를 막으면 안 됨
                logger.exception("초안 발송 처리 실패: draft_id=%s", draft.id)
                results[draft.id] = f"error: {exc}"
        return results

    def send_now(self, draft_id: int) -> str:
        """단일 초안을 지금 바로 발송한다(`outbox`/`sending` 상태여야 한다)."""
        conn = self._db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        if draft is None:
            raise PermanentError(f"초안을 찾을 수 없습니다: {draft_id}")
        return self._send_one(draft)

    # --- 내부 구현 --------------------------------------------------------

    def _send_one(self, draft: DraftRow) -> str:
        if draft.status == "outbox":
            if not drafts_repo.mark_sending(self._db, draft.id):
                return "skipped(already taken)"
        elif draft.status != "sending":
            raise PermanentError(
                f"발송 대상이 아닌 상태입니다(draft_id={draft.id}): {draft.status}"
            )

        conn = self._db.new_read_connection()
        try:
            account = accounts_repo.get_account(conn, draft.account_id)
        finally:
            conn.close()
        if account is None:
            drafts_repo.mark_failed(self._db, draft.id, last_error="계정을 찾을 수 없음")
            return "failed"

        now_iso = self._clock.now().isoformat()
        logger.info("초안 %s 발송 시작: account=%s", draft.id, account.id)
        try:
            built = self._build_final_message(account, draft)
            password = self._credential_provider_factory(account.auth_method).get_password(
                account.id
            )
            rcpt_tos = [*built.to_addrs, *built.cc_addrs, *built.bcc_addrs]
            smtp_outgoing.send_mail(
                host=account.out_host,
                port=account.out_port,
                username=account.effective_out_username,
                password=password,
                security=account.out_security,
                mail_from=account.email_address,
                rcpt_tos=rcpt_tos,
                raw_bytes=built.raw_bytes,
            )
        except AuthError as exc:
            logger.warning("초안 %s 발송 실패(인증 오류): %s", draft.id, exc)
            drafts_repo.mark_failed(self._db, draft.id, last_error=str(exc))
            self._publish_account_error(draft.account_id, str(exc), now_iso)
            self._publish_send_failed(draft.id, str(exc), now_iso)
            return "failed(auth)"
        except PermanentError as exc:
            logger.warning("초안 %s 발송 실패: %s", draft.id, exc)
            drafts_repo.mark_failed(self._db, draft.id, last_error=str(exc))
            self._publish_account_error(draft.account_id, str(exc), now_iso)
            self._publish_send_failed(draft.id, str(exc), now_iso)
            return "failed"
        except TransientError as exc:
            logger.warning("초안 %s 발송 일시 오류, 재시도 예정: %s", draft.id, exc)
            self._retry_or_fail(draft, str(exc))
            self._publish_account_error(draft.account_id, str(exc), now_iso)
            return "retry_or_failed"

        self._append_to_sent(account, built.raw_bytes)
        sent_message_id = self._store_sent_copy(account, built.raw_bytes)
        drafts_repo.mark_sent(self._db, draft.id, sent_message_id=sent_message_id)
        logger.info("초안 %s 발송 완료", draft.id)
        self._publish(EVENT_ACCOUNT_CONNECTED, {"account_id": draft.account_id, "at": now_iso})
        self._publish(EVENT_SEND_COMPLETED, {"draft_id": draft.id, "at": now_iso})
        return "sent"

    def _publish_account_error(self, account_id: int, error: str, at: str) -> None:
        self._publish(EVENT_ACCOUNT_ERROR, {"account_id": account_id, "error": error, "at": at})

    def _publish_send_failed(self, draft_id: int, error: str, at: str) -> None:
        self._publish(EVENT_SEND_FAILED, {"draft_id": draft_id, "error": error, "at": at})

    def _retry_or_fail(self, draft: DraftRow, error: str) -> None:
        attempts = draft.send_attempts + 1
        if attempts >= MAX_SEND_ATTEMPTS:
            drafts_repo.mark_failed(self._db, draft.id, last_error=error)
            return
        backoff = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
        next_send_at = (self._clock.now() + backoff).isoformat()
        drafts_repo.mark_retry(self._db, draft.id, next_send_at=next_send_at, last_error=error)

    def _build_final_message(self, account: Account, draft: DraftRow) -> mime_build.BuiltMessage:
        attachments = [self._resolve_attachment(item) for item in draft.attachments]
        in_reply_to, references = self._threading_headers(draft)
        return mime_build.build_new(
            from_addr=account.email_address,
            from_name=account.sender_name or account.display_name,
            to_addrs=draft.to_addrs,
            cc_addrs=draft.cc_addrs,
            bcc_addrs=draft.bcc_addrs,
            subject=draft.subject or "",
            body_text=draft.body_text or "",
            attachments=attachments,
            in_reply_to=in_reply_to,
            references=references,
        )

    def _threading_headers(self, draft: DraftRow) -> tuple[str | None, list[str]]:
        if draft.kind not in _REPLY_KINDS or draft.source_message_id is None:
            return None, []
        conn = self._db.new_read_connection()
        try:
            source = messages_repo.get_message(conn, draft.source_message_id)
        finally:
            conn.close()
        if source is None:
            return None, []
        references = source.references_hdr.split() if source.references_hdr else []
        if source.message_id_hdr and source.message_id_hdr not in references:
            references.append(source.message_id_hdr)
        return source.message_id_hdr, references

    def _resolve_attachment(self, item: dict) -> OutgoingAttachment:
        source = item.get("source")
        if source == "original_part":
            return self._resolve_original_part(item)
        if source == "original_eml":
            return self._resolve_original_eml(item)
        if source == "local_file":
            return self._resolve_local_file(item)
        raise PermanentError(f"알 수 없는 첨부 소스입니다: {source}")

    def _resolve_local_file(self, item: dict) -> OutgoingAttachment:
        """작성 창에서 사용자가 직접 첨부한 파일(§11.2) — `draft_service.stage_local_attachment`가
        미리 `mail_blob_dir`(outgoing/) 안으로 복사해 둔 경로를 그대로 읽는다.
        """
        path = Path(item["path"])
        if not path.is_file():
            raise PermanentError(f"첨부 파일을 찾을 수 없습니다: {path}")
        payload = path.read_bytes()
        filename = item.get("filename") or path.name
        content_type = item.get("content_type")
        return OutgoingAttachment(filename=filename, content_type=content_type, payload=payload)

    def _resolve_original_part(self, item: dict) -> OutgoingAttachment:
        raw, _row = self._read_source_eml(item["message_id"])
        parsed = parse_eml(raw)
        match = next((a for a in parsed.attachments if a.part_index == item["part_index"]), None)
        if match is None:
            raise PermanentError(f"첨부 파트를 찾을 수 없습니다: {item.get('part_index')}")
        filename = item.get("filename") or "attachment"
        content_type = item.get("content_type") or match.content_type
        return OutgoingAttachment(
            filename=filename, content_type=content_type, payload=match.payload
        )

    def _resolve_original_eml(self, item: dict) -> OutgoingAttachment:
        raw, _row = self._read_source_eml(item["message_id"])
        filename = item.get("filename") or "forwarded_message.eml"
        return OutgoingAttachment(filename=filename, content_type="message/rfc822", payload=raw)

    def _read_source_eml(self, message_id: int) -> tuple[bytes, messages_repo.MessageRow]:
        conn = self._db.new_read_connection()
        try:
            row = messages_repo.get_message(conn, message_id)
        finally:
            conn.close()
        if row is None:
            raise PermanentError(f"첨부 원본 메일을 찾을 수 없습니다: {message_id}")
        return Path(row.eml_path).read_bytes(), row

    def _append_to_sent(self, account: Account, raw_bytes: bytes) -> None:
        """IMAP 계정은 발송 후 Sent 폴더에 APPEND한다. 실패해도 발송 자체는 성공 처리한다(§2(b))."""
        if account.incoming_protocol != "imap":
            return
        provider: ImapIncomingProvider | None = None
        try:
            password = self._credential_provider_factory(account.auth_method).get_password(
                account.id
            )
            provider = ImapIncomingProvider(
                host=account.in_host,
                port=account.in_port,
                username=account.in_username,
                password=password,
                security=account.in_security,
            )
            provider.connect()
            sent_folder = provider.find_role_folder("sent") or "Sent"
            provider.append_message(sent_folder, raw_bytes)
        except EmailToMcpError:
            logger.warning("IMAP Sent 폴더 APPEND 실패(발송 자체는 성공 처리함)", exc_info=True)
        finally:
            if provider is not None:
                provider.close()

    def _store_sent_copy(self, account: Account, raw_bytes: bytes) -> int | None:
        """로컬 `보낸편지함`에도 사본을 남긴다(서버 폴더 동기화와 별개로 즉시 반영)."""
        try:
            sent_folder = folders_repo.get_or_create_folder(
                self._db, account.id, "Sent", role="sent"
            )
            parsed = parse_eml(raw_bytes)
            received_at = self._clock.now()
            data = SyncService.to_message_insert(
                account_id=account.id,
                folder_id=sent_folder.id,
                parsed=parsed,
                remote_uid=None,
                eml_path=self._write_sent_eml(raw_bytes),
                received_at=received_at,
                is_read=True,
                is_flagged=False,
            )
            return messages_repo.insert_message(self._db, data)
        except Exception:  # noqa: BLE001 — 로컬 Sent 사본 실패는 발송 결과에 영향 없음
            logger.warning("로컬 보낸편지함 사본 저장 실패", exc_info=True)
            return None

    def _write_sent_eml(self, raw_bytes: bytes) -> str:
        path = self._mail_blob_dir / f"{uuid.uuid4()}.eml"
        path.write_bytes(raw_bytes)
        return str(path)
