"""초안 생성 단일 창구 (DESIGN.md §4.1 §6.3 create_draft/create_reply_draft/create_forward_draft).

UI/MCP/ReplyPolicy가 초안을 만들 때는 모두 이 서비스를 거친다. 회신/전달은 원본
`.eml`을 다시 파싱해 실제 첨부 바이트까지 갖춘 `ParsedMessage`를 만들고,
`mail.mime.build`로 수신자·제목·본문을 한 번에 결정한 뒤, 그 결과(및 첨부 메타)만
`drafts` 테이블에 저장한다 — 최종 발신 MIME 조립(및 사용자가 고친 값 반영)은
`send_service`가 발송 시점에 다시 한다.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from emailtomcp.core.errors import PermanentError
from emailtomcp.mail.addressing import addr_domain_norm, normalize_addr
from emailtomcp.mail.attachments_safety import sanitize_filename
from emailtomcp.mail.mime import build as mime_build
from emailtomcp.mail.mime.build import OutgoingAttachment
from emailtomcp.mail.mime.parse import parse_eml
from emailtomcp.mail.models import ParsedAttachment, ParsedMessage
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import drafts as drafts_repo
from emailtomcp.storage.repositories import messages as messages_repo
from emailtomcp.storage.repositories.drafts import RecipientNorm

if TYPE_CHECKING:
    from emailtomcp.storage.db import Database
    from emailtomcp.storage.repositories.accounts import Account

# pending_approval 만료(§6.2 C-04). 이 서비스는 값만 계산해 둔다 — 실제 승인 플로우는 P2.
APPROVAL_EXPIRY = timedelta(hours=24)

# 작성 창에서 사용자가 직접 첨부한 파일을 보관하는 하위 폴더명(§11.2).
OUTGOING_ATTACHMENTS_SUBDIR = "outgoing_attachments"


def _attachment_meta_original_part(message_id: int, att: ParsedAttachment) -> dict:
    """ "원본 첨부"를 참조하는 메타 — 실제 바이트는 보내는 시점에 원본 `.eml`에서
    `part_index`로 다시 뽑는다(§8.5, drafts에는 바이트를 두지 않는다).
    """
    return {
        "source": "original_part",
        "message_id": message_id,
        "part_index": att.part_index,
        "filename": sanitize_filename(att.filename_raw) if att.filename_raw else "attachment",
        "content_type": att.content_type,
        "size_bytes": att.size_bytes,
        "sha256": hashlib.sha256(att.payload).hexdigest() if att.payload else None,
    }


def compute_snapshot_hash(
    *,
    to_addrs: list[str],
    cc_addrs: list[str],
    bcc_addrs: list[str],
    subject: str | None,
    body_text: str | None,
    attachments: list[dict],
) -> str:
    """승인 바인딩용 스냅샷 해시(DESIGN.md §6.3, §11.6 H9).

    To/CC/BCC/제목/본문/첨부명·크기·sha256을 정규화해 sha256 한다. 이 함수는
    승인 플로우(P2)에서 쓸 함수만 미리 만들어 둔 것이다 — 이번 범위에서는 호출하지 않는다.
    """

    def _norm_addr_list(addrs: list[str]) -> list[str]:
        return sorted(normalize_addr(a) for a in addrs)

    attachments_part = sorted(
        f"{a.get('filename', '')}|{a.get('size_bytes', 0)}|{a.get('sha256', '')}"
        for a in attachments
    )
    payload = {
        "to": _norm_addr_list(to_addrs),
        "cc": _norm_addr_list(cc_addrs),
        "bcc": _norm_addr_list(bcc_addrs),
        "subject": unicodedata.normalize("NFKC", subject or ""),
        "body": unicodedata.normalize("NFKC", body_text or ""),
        "attachments": attachments_part,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DraftService:
    """초안 생성 단일 창구."""

    def __init__(self, db: Database, *, mail_blob_dir: Path | None = None) -> None:
        self._db = db
        self._mail_blob_dir = mail_blob_dir

    # --- 공개 API --------------------------------------------------------

    def create_draft(
        self,
        *,
        account_id: int,
        to_addrs: list[str],
        subject: str | None,
        body_text: str | None,
        cc_addrs: list[str] | None = None,
        bcc_addrs: list[str] | None = None,
        attachments: list[dict] | None = None,
        origin: str = "user",
    ) -> int:
        """새 메일(kind='new') 초안을 만든다."""
        to_addrs = mime_build.dedupe_addrs(to_addrs)
        cc_addrs = mime_build.dedupe_addrs(cc_addrs or [])
        bcc_addrs = mime_build.dedupe_addrs(bcc_addrs or [])
        return drafts_repo.create_draft(
            self._db,
            account_id=account_id,
            kind="new",
            origin=origin,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            bcc_addrs=bcc_addrs,
            subject=subject,
            body_text=body_text,
            attachments=attachments or [],
            recipient_norms=self._recipient_norms(to_addrs, cc_addrs, bcc_addrs),
        )

    def create_reply_draft(
        self,
        *,
        account_id: int,
        source_message_id: int,
        body_text: str,
        reply_all: bool = False,
        include_attachments: bool = True,
        origin: str = "user",
    ) -> int:
        """회신/전체회신(kind='reply'|'reply_all') 초안을 만든다.

        원본 첨부파일을 기본으로 포함한다(`include_attachments=True`, §1.2 #23).
        """
        account, parsed = self._load_account_and_source(account_id, source_message_id)

        original_attachments = None
        attachments_meta: list[dict] = []
        if include_attachments:
            non_inline = [a for a in parsed.attachments if not a.is_inline]
            original_attachments = [self._to_outgoing_attachment(a) for a in non_inline]
            attachments_meta = [
                _attachment_meta_original_part(source_message_id, a) for a in non_inline
            ]

        built = mime_build.build_reply(
            parsed,
            from_addr=account.email_address,
            from_name=account.sender_name or account.display_name,
            body_text=body_text,
            reply_all=reply_all,
            self_aliases=account.aliases,
            include_attachments=include_attachments,
            original_attachments=original_attachments,
        )

        kind = "reply_all" if reply_all else "reply"
        return drafts_repo.create_draft(
            self._db,
            account_id=account_id,
            kind=kind,
            origin=origin,
            to_addrs=built.to_addrs,
            cc_addrs=built.cc_addrs,
            bcc_addrs=built.bcc_addrs,
            subject=built.subject,
            body_text=built.body_text,
            attachments=attachments_meta,
            source_message_id=source_message_id,
            recipient_norms=self._recipient_norms(built.to_addrs, built.cc_addrs, built.bcc_addrs),
        )

    def create_forward_draft(
        self,
        *,
        account_id: int,
        source_message_id: int,
        to_addrs: list[str],
        body_text: str = "",
        mode: str = "inline",
        cc_addrs: list[str] | None = None,
        bcc_addrs: list[str] | None = None,
        origin: str = "user",
    ) -> int:
        """전달(인라인/첨부로, kind='forward_inline'|'forward_attach') 초안을 만든다."""
        if mode not in ("inline", "attach"):
            raise PermanentError(f"알 수 없는 전달 모드입니다: {mode}")

        account, parsed = self._load_account_and_source(account_id, source_message_id)
        to_addrs = mime_build.dedupe_addrs(to_addrs)
        cc_addrs = mime_build.dedupe_addrs(cc_addrs or [])
        bcc_addrs = mime_build.dedupe_addrs(bcc_addrs or [])

        if mode == "inline":
            non_inline = [a for a in parsed.attachments if not a.is_inline]
            original_attachments = [self._to_outgoing_attachment(a) for a in non_inline]
            attachments_meta = [
                _attachment_meta_original_part(source_message_id, a) for a in non_inline
            ]
            built = mime_build.build_forward_inline(
                parsed,
                from_addr=account.email_address,
                from_name=account.sender_name or account.display_name,
                to_addrs=to_addrs,
                cc_addrs=cc_addrs,
                bcc_addrs=bcc_addrs,
                body_text=body_text,
                original_attachments=original_attachments,
            )
            kind = "forward_inline"
        else:
            raw = self._read_source_raw(source_message_id)
            built = mime_build.build_forward_attach(
                parsed,
                raw,
                from_addr=account.email_address,
                from_name=account.sender_name or account.display_name,
                to_addrs=to_addrs,
                cc_addrs=cc_addrs,
                bcc_addrs=bcc_addrs,
                body_text=body_text,
            )
            fallback_name = "forwarded_message"
            forward_filename = (parsed.subject or fallback_name).strip() or fallback_name
            attachments_meta = [
                {
                    "source": "original_eml",
                    "message_id": source_message_id,
                    "filename": f"{forward_filename}.eml",
                }
            ]
            kind = "forward_attach"

        return drafts_repo.create_draft(
            self._db,
            account_id=account_id,
            kind=kind,
            origin=origin,
            to_addrs=built.to_addrs,
            cc_addrs=built.cc_addrs,
            bcc_addrs=built.bcc_addrs,
            subject=built.subject,
            body_text=built.body_text,
            attachments=attachments_meta,
            source_message_id=source_message_id,
            recipient_norms=self._recipient_norms(built.to_addrs, built.cc_addrs, built.bcc_addrs),
        )

    def update_compose(
        self,
        draft_id: int,
        *,
        to_addrs: list[str],
        cc_addrs: list[str] | None = None,
        bcc_addrs: list[str] | None = None,
        subject: str | None,
        body_text: str | None,
        attachments: list[dict] | None = None,
        mcp_owner_token_id: int | None = None,
    ) -> bool:
        """작성 창의 자동저장/수동저장 공용 창구. `status='draft'`일 때만 반영된다(§5.3).

        MCP `update_draft`는 `mcp_owner_token_id`를 넘겨, writer 트랜잭션 안에서도 그 토큰이
        만든 MCP 초안일 때만 반영되게 한다(보안검토 N-1).
        """
        to_addrs = mime_build.dedupe_addrs(to_addrs)
        cc_addrs = mime_build.dedupe_addrs(cc_addrs or [])
        bcc_addrs = mime_build.dedupe_addrs(bcc_addrs or [])
        return drafts_repo.update_compose(
            self._db,
            draft_id,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            bcc_addrs=bcc_addrs,
            subject=subject,
            body_text=body_text,
            attachments=attachments or [],
            recipient_norms=self._recipient_norms(to_addrs, cc_addrs, bcc_addrs),
            mcp_owner_token_id=mcp_owner_token_id,
        )

    def save_compose_and_mark_outbox(
        self,
        draft_id: int,
        *,
        to_addrs: list[str],
        cc_addrs: list[str] | None = None,
        bcc_addrs: list[str] | None = None,
        subject: str | None,
        body_text: str | None,
        attachments: list[dict] | None = None,
        message_id_hdr: str,
        approved_by: str,
    ) -> bool:
        """작성 창 [보내기] — 화면 내용 저장과 `draft → outbox` 전이를 원자적으로 한다
        (보안검토 N-1/R-1). 저장이 실패(예외)하면 전이도 일어나지 않는다."""
        to_addrs = mime_build.dedupe_addrs(to_addrs)
        cc_addrs = mime_build.dedupe_addrs(cc_addrs or [])
        bcc_addrs = mime_build.dedupe_addrs(bcc_addrs or [])
        return drafts_repo.save_compose_and_mark_outbox(
            self._db,
            draft_id,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            bcc_addrs=bcc_addrs,
            subject=subject,
            body_text=body_text,
            attachments=attachments or [],
            recipient_norms=self._recipient_norms(to_addrs, cc_addrs, bcc_addrs),
            message_id_hdr=message_id_hdr,
            approved_by=approved_by,
        )

    def discard_draft(self, draft_id: int, *, mcp_owner_token_id: int | None = None) -> bool:
        """작성 중(`status='draft'`) 초안을 지운다(발송 이력이 있는 초안은 보존된다)."""
        return drafts_repo.delete_draft(self._db, draft_id, mcp_owner_token_id=mcp_owner_token_id)

    def stage_local_attachment(self, file_path: str) -> dict:
        """작성 창에서 사용자가 드래그앤드롭/파일선택으로 추가한 첨부를 안전하게 보관한다
        (§11.2). 원본 메일 첨부(`original_part`/`original_eml`)와 달리 발신자 로컬
        파일시스템에만 있던 바이트이므로, 보내는 시점에 다시 읽을 수 있도록
        `mail_blob_dir` 아래로 복사해 둔다.
        """
        if self._mail_blob_dir is None:
            raise PermanentError("mail_blob_dir가 설정되지 않았습니다(DraftService 구성 오류)")
        source = Path(file_path)
        if not source.is_file():
            raise PermanentError(f"첨부할 파일을 찾을 수 없습니다: {file_path}")
        payload = source.read_bytes()
        safe_name = sanitize_filename(source.name)
        dest_dir = self._mail_blob_dir / OUTGOING_ATTACHMENTS_SUBDIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / f"{uuid.uuid4()}_{safe_name}"
        dest_path.write_bytes(payload)
        return {
            "source": "local_file",
            "path": str(dest_path),
            "filename": safe_name,
            "content_type": mimetypes.guess_type(safe_name)[0],
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def set_approval_hash(self, draft_id: int) -> str:
        """현재 초안 스냅샷으로 승인 해시를 계산하고 저장한다(P2 발송 승인에서 쓸 준비 작업)."""
        conn = self._db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        if draft is None:
            raise PermanentError(f"초안을 찾을 수 없습니다: {draft_id}")

        snapshot_hash = compute_snapshot_hash(
            to_addrs=draft.to_addrs,
            cc_addrs=draft.cc_addrs,
            bcc_addrs=draft.bcc_addrs,
            subject=draft.subject,
            body_text=draft.body_text,
            attachments=draft.attachments,
        )
        expires_at = (datetime.now(UTC) + APPROVAL_EXPIRY).isoformat()
        drafts_repo.set_approval_hash(self._db, draft_id, snapshot_hash, expires_at)
        return snapshot_hash

    # --- 내부 구현 --------------------------------------------------------

    def _recipient_norms(
        self, to_addrs: list[str], cc_addrs: list[str], bcc_addrs: list[str]
    ) -> list[RecipientNorm]:
        norms: list[RecipientNorm] = []
        for kind, addrs in (("to", to_addrs), ("cc", cc_addrs), ("bcc", bcc_addrs)):
            for addr in addrs:
                norms.append(
                    RecipientNorm(
                        kind=kind,
                        addr_norm=normalize_addr(addr),
                        domain_norm=addr_domain_norm(addr),
                    )
                )
        return norms

    def _load_account_and_source(
        self, account_id: int, source_message_id: int
    ) -> tuple[Account, ParsedMessage]:
        conn = self._db.new_read_connection()
        try:
            account = accounts_repo.get_account(conn, account_id)
            source_row = messages_repo.get_message(conn, source_message_id)
        finally:
            conn.close()
        if account is None:
            raise PermanentError(f"계정을 찾을 수 없습니다: {account_id}")
        if source_row is None:
            raise PermanentError(f"원본 메일을 찾을 수 없습니다: {source_message_id}")

        raw = Path(source_row.eml_path).read_bytes()
        parsed = parse_eml(raw)
        return account, parsed

    def _read_source_raw(self, source_message_id: int) -> bytes:
        conn = self._db.new_read_connection()
        try:
            source_row = messages_repo.get_message(conn, source_message_id)
        finally:
            conn.close()
        if source_row is None:
            raise PermanentError(f"원본 메일을 찾을 수 없습니다: {source_message_id}")
        return Path(source_row.eml_path).read_bytes()

    @staticmethod
    def _to_outgoing_attachment(att: ParsedAttachment) -> OutgoingAttachment:
        filename = sanitize_filename(att.filename_raw) if att.filename_raw else "attachment"
        return OutgoingAttachment(
            filename=filename, content_type=att.content_type, payload=att.payload
        )
