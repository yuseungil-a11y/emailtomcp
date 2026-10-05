"""draft 스코프 도구와 manage 스코프 도구 (DESIGN.md §6.3).

draft: create_draft, create_reply_draft, create_forward_draft, update_draft, delete_draft
manage: mark_read, set_flag, delete_message (move_message는 P4)

- 초안 생성·수정·삭제는 P1의 `mail.draft_service.DraftService`를 그대로 호출한다
  (초안 생성 단일 창구, 새로 만들지 않는다). 출처는 `origin='mcp'`로 기록한다.
- `update_draft`/`delete_draft`(와 `send_draft`)는 **MCP가 만든 초안(`origin='mcp'`)이고
  같은 토큰(`mcp_token_id`)으로 만든 것**에만 쓸 수 있다(보안검토 H-1). 사용자가 UI에서
  쓰던 초안을 read+draft 스코프만으로 찾아(`list_drafts`) 수신자·본문을 바꿔 두면, 사용자가
  나중에 [보내기]를 누를 때 승인·레이트리밋 없이 그대로 나가기 때문이다.
- `update_draft`/`delete_draft`는 `status='draft'`일 때만 허용한다. 특히
  `pending_approval`(승인 대기)에서는 거부한다 — 승인 대기 중 수신자·본문을 바꾸는
  TOCTOU를 막기 위해서다(H9). 상태 조건은 SQL(`WHERE status='draft'`)에도 걸려 있어
  확인과 변경 사이의 경합도 없다.
- 주소는 addr-spec(`local@domain`)만 받는다. 제목에는 줄바꿈을 허용하지 않는다(헤더
  인젝션 방지).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator

from emailtomcp.core.errors import PermanentError, PolicyError
from emailtomcp.core.models import DraftOrigin, DraftStatus
from emailtomcp.mcp_server.tool_base import StrictArgs, ToolContext, ToolSpec, run_blocking
from emailtomcp.mcp_server.tools_read import read_with_conn
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import drafts as drafts_repo
from emailtomcp.storage.repositories import folders as folders_repo
from emailtomcp.storage.repositories import messages as messages_repo

if TYPE_CHECKING:
    from emailtomcp.mcp_server.auth import Principal
    from emailtomcp.storage.repositories.drafts import DraftRow

MAX_RECIPIENTS = 50
MAX_SUBJECT = 998
MAX_BODY = 500_000

_ADDR_RE = re.compile(r"^[^@\s,;<>\"()\[\]\\]+@[^@\s,;<>\"()\[\]\\]+\.[^@\s,;<>\"()\[\]\\]+$")


def _check_addrs(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    cleaned = [v.strip() for v in values]
    bad = [v for v in cleaned if not _ADDR_RE.match(v) or len(v) > 320]
    if bad:
        raise ValueError(f"주소 형식이 올바르지 않습니다: {', '.join(bad[:5])}")
    return cleaned


def _check_subject(value: str | None) -> str | None:
    if value is not None and ("\r" in value or "\n" in value):
        raise ValueError("제목에는 줄바꿈을 넣을 수 없습니다")
    return value


class _AddrFields(StrictArgs):
    @field_validator("to", "cc", "bcc", mode="after", check_fields=False)
    @classmethod
    def _validate_addrs(cls, value: list[str] | None) -> list[str] | None:
        return _check_addrs(value)

    @field_validator("subject", mode="after", check_fields=False)
    @classmethod
    def _validate_subject(cls, value: str | None) -> str | None:
        return _check_subject(value)


class CreateDraftArgs(_AddrFields):
    account_id: int = Field(ge=1)
    to: list[str] = Field(min_length=1, max_length=MAX_RECIPIENTS)
    cc: list[str] | None = Field(default=None, max_length=MAX_RECIPIENTS)
    bcc: list[str] | None = Field(default=None, max_length=MAX_RECIPIENTS)
    subject: str = Field(default="", max_length=MAX_SUBJECT)
    body_text: str = Field(default="", max_length=MAX_BODY)


class CreateReplyDraftArgs(StrictArgs):
    message_id: int = Field(ge=1)
    body_text: str = Field(default="", max_length=MAX_BODY)
    reply_all: bool = False
    include_attachments: bool = True


class CreateForwardDraftArgs(_AddrFields):
    message_id: int = Field(ge=1)
    to: list[str] = Field(min_length=1, max_length=MAX_RECIPIENTS)
    cc: list[str] | None = Field(default=None, max_length=MAX_RECIPIENTS)
    note: str = Field(default="", max_length=MAX_BODY)
    mode: Literal["inline", "attach"] = "inline"


class UpdateDraftArgs(_AddrFields):
    draft_id: int = Field(ge=1)
    to: list[str] | None = Field(default=None, max_length=MAX_RECIPIENTS)
    cc: list[str] | None = Field(default=None, max_length=MAX_RECIPIENTS)
    bcc: list[str] | None = Field(default=None, max_length=MAX_RECIPIENTS)
    subject: str | None = Field(default=None, max_length=MAX_SUBJECT)
    body_text: str | None = Field(default=None, max_length=MAX_BODY)


class DraftIdOnlyArgs(StrictArgs):
    draft_id: int = Field(ge=1)


class MessageIdsFlagArgs(StrictArgs):
    message_ids: list[int] = Field(min_length=1, max_length=100)
    value: bool = True


class MessageIdArgs(StrictArgs):
    message_id: int = Field(ge=1)


# ---------------------------------------------------------------- 공통


def _get_draft(tc: ToolContext, draft_id: int) -> DraftRow:
    draft = read_with_conn(tc.db, lambda conn: drafts_repo.get_draft(conn, draft_id))
    if draft is None:
        raise PermanentError(f"초안을 찾을 수 없습니다: {draft_id}")
    return draft


def require_mcp_owned(draft: DraftRow, principal: Principal, action: str) -> None:
    """MCP 도구가 손댈 수 있는 초안인지 확인한다(보안검토 H-1).

    - `origin='mcp'`가 아니면(사용자가 UI에서 쓴 초안, 자동회신 초안 등) 거부한다.
    - 초안을 만든 토큰(`mcp_token_id`)과 지금 요청한 토큰이 다르면 거부한다 — 다른 MCP
      클라이언트가 만든 초안도 건드릴 수 없다. 토큰을 재발급하면 이전 토큰으로 만든 초안은
      MCP로는 더 이상 수정·삭제·발송할 수 없고 사용자가 앱에서 처리한다.

    - 요청 주체에 토큰 ID가 없으면 거부한다(fail-closed, 보안검토 N-4). 초안 쪽
      `mcp_token_id`도 NULL일 때 `None == None`으로 통과하지 않게 하기 위함이다.

    `origin`은 사용자가 [수정 후 발송]으로 초안을 넘겨받을 때 'user'로 바뀐다(보안검토
    N-1). 그래서 이 함수(읽기 스냅샷 기준 사전 확인)만으로는 충분하지 않다 — 실제 쓰기는
    writer 트랜잭션 안에서 소유권을 다시 확인한다(`update_compose`/`delete_draft`의
    `mcp_owner_token_id` 조건, `send_draft`의 writer 내 재확인).
    """
    if principal.token_id is None:
        raise PolicyError(f"토큰으로 식별되지 않은 연결은 초안을 {action}할 수 없습니다.")
    if draft.origin != DraftOrigin.MCP:
        raise PolicyError(
            f"사용자가 앱에서 직접 작성한 초안은 MCP 도구로 {action}할 수 없습니다"
            "(Claude가 만든 초안만 다룰 수 있습니다)."
        )
    if draft.mcp_token_id != principal.token_id:
        raise PolicyError(
            f"다른 MCP 연결(토큰)이 만든 초안은 {action}할 수 없습니다. "
            "이 연결로 만든 초안만 다룰 수 있습니다."
        )


def _require_editable(draft: DraftRow, action: str) -> None:
    if draft.status == DraftStatus.PENDING_APPROVAL:
        raise PolicyError(
            f"승인 대기 중인 초안은 {action}할 수 없습니다. 사용자가 앱에서 승인을 거부하거나 "
            "[수정 후 발송]을 선택해야 다시 편집할 수 있습니다."
        )
    if draft.status != DraftStatus.DRAFT:
        raise PolicyError(
            f"작성 중(draft) 상태의 초안만 {action}할 수 있습니다(현재: {draft.status})"
        )


def _source_account_id(tc: ToolContext, message_id: int) -> int:
    row = read_with_conn(tc.db, lambda conn: messages_repo.get_message(conn, message_id))
    if row is None:
        raise PermanentError(f"원본 메일을 찾을 수 없습니다: {message_id}")
    return row.account_id


def _tag_token(tc: ToolContext, draft_id: int, principal: Principal) -> None:
    if principal.token_id is not None:
        drafts_repo.set_mcp_token_id(tc.db, draft_id, principal.token_id)


def _draft_result(tc: ToolContext, draft_id: int) -> dict[str, Any]:
    draft = _get_draft(tc, draft_id)
    return {
        "draft_id": draft.id,
        "status": draft.status,
        "kind": draft.kind,
        "to": draft.to_addrs,
        "cc": draft.cc_addrs,
        "bcc": draft.bcc_addrs,
        "attachments": [a.get("filename") for a in draft.attachments],
    }


# ---------------------------------------------------------------- draft 스코프


async def create_draft(tc: ToolContext, p: Principal, args: CreateDraftArgs) -> dict[str, Any]:
    def _work() -> dict[str, Any]:
        exists = read_with_conn(
            tc.db, lambda conn: accounts_repo.get_account(conn, args.account_id) is not None
        )
        if not exists:
            raise PermanentError(f"계정을 찾을 수 없습니다: {args.account_id}")
        draft_id = tc.draft_service.create_draft(
            account_id=args.account_id,
            to_addrs=args.to,
            cc_addrs=args.cc or [],
            bcc_addrs=args.bcc or [],
            subject=args.subject,
            body_text=args.body_text,
            origin=DraftOrigin.MCP,
        )
        _tag_token(tc, draft_id, p)
        return _draft_result(tc, draft_id)

    return await run_blocking(_work)


async def create_reply_draft(
    tc: ToolContext, p: Principal, args: CreateReplyDraftArgs
) -> dict[str, Any]:
    def _work() -> dict[str, Any]:
        draft_id = tc.draft_service.create_reply_draft(
            account_id=_source_account_id(tc, args.message_id),
            source_message_id=args.message_id,
            body_text=args.body_text,
            reply_all=args.reply_all,
            include_attachments=args.include_attachments,
            origin=DraftOrigin.MCP,
        )
        _tag_token(tc, draft_id, p)
        return _draft_result(tc, draft_id)

    return await run_blocking(_work)


async def create_forward_draft(
    tc: ToolContext, p: Principal, args: CreateForwardDraftArgs
) -> dict[str, Any]:
    def _work() -> dict[str, Any]:
        draft_id = tc.draft_service.create_forward_draft(
            account_id=_source_account_id(tc, args.message_id),
            source_message_id=args.message_id,
            to_addrs=args.to,
            cc_addrs=args.cc or [],
            body_text=args.note,
            mode=args.mode,
            origin=DraftOrigin.MCP,
        )
        _tag_token(tc, draft_id, p)
        return _draft_result(tc, draft_id)

    return await run_blocking(_work)


async def update_draft(tc: ToolContext, p: Principal, args: UpdateDraftArgs) -> dict[str, Any]:
    if all(v is None for v in (args.to, args.cc, args.bcc, args.subject, args.body_text)):
        raise PermanentError("바꿀 항목(to/cc/bcc/subject/body_text)을 하나 이상 지정하세요")

    def _work() -> dict[str, Any]:
        draft = _get_draft(tc, args.draft_id)
        require_mcp_owned(draft, p, "수정")
        _require_editable(draft, "수정")
        changed = tc.draft_service.update_compose(
            args.draft_id,
            to_addrs=args.to if args.to is not None else draft.to_addrs,
            cc_addrs=args.cc if args.cc is not None else draft.cc_addrs,
            bcc_addrs=args.bcc if args.bcc is not None else draft.bcc_addrs,
            subject=args.subject if args.subject is not None else draft.subject,
            body_text=args.body_text if args.body_text is not None else draft.body_text,
            attachments=draft.attachments,
            # writer 트랜잭션 안에서도 소유권을 확인한다(사전 확인 이후 사용자가 넘겨받았을
            # 수 있다, 보안검토 N-1). require_mcp_owned를 통과했으므로 token_id는 None이 아니다.
            mcp_owner_token_id=p.token_id,
        )
        if not changed:
            # 확인과 갱신 사이에 상태가 바뀐 경우(예: 그 사이 send_draft) — SQL 조건이 막았다.
            current = _get_draft(tc, args.draft_id)
            require_mcp_owned(current, p, "수정")
            _require_editable(current, "수정")
            raise PolicyError("초안 상태가 바뀌어 수정하지 못했습니다")
        return _draft_result(tc, args.draft_id)

    return await run_blocking(_work)


async def delete_draft(tc: ToolContext, p: Principal, args: DraftIdOnlyArgs) -> dict[str, Any]:
    def _work() -> dict[str, Any]:
        draft = _get_draft(tc, args.draft_id)
        require_mcp_owned(draft, p, "삭제")
        _require_editable(draft, "삭제")
        if not tc.draft_service.discard_draft(args.draft_id, mcp_owner_token_id=p.token_id):
            current = _get_draft(tc, args.draft_id)
            require_mcp_owned(current, p, "삭제")
            _require_editable(current, "삭제")
            raise PolicyError("초안 상태가 바뀌어 삭제하지 못했습니다")
        return {"draft_id": args.draft_id, "deleted": True}

    return await run_blocking(_work)


# ---------------------------------------------------------------- manage 스코프


def _existing_ids(tc: ToolContext, ids: list[int]) -> tuple[list[int], list[int]]:
    def _work(conn: Any) -> tuple[list[int], list[int]]:
        found: list[int] = []
        missing: list[int] = []
        for message_id in dict.fromkeys(ids):
            (found if messages_repo.get_message(conn, message_id) else missing).append(message_id)
        return found, missing

    return read_with_conn(tc.db, _work)


async def mark_read(tc: ToolContext, _p: Principal, args: MessageIdsFlagArgs) -> dict[str, Any]:
    found, missing = await run_blocking(_existing_ids, tc, args.message_ids)
    for message_id in found:
        await tc.mail_actions.set_read(message_id, args.value)
    return {"updated": found, "missing": missing, "value": args.value}


async def set_flag(tc: ToolContext, _p: Principal, args: MessageIdsFlagArgs) -> dict[str, Any]:
    found, missing = await run_blocking(_existing_ids, tc, args.message_ids)
    for message_id in found:
        await tc.mail_actions.set_flagged(message_id, args.value)
    return {"updated": found, "missing": missing, "value": args.value}


async def delete_message(tc: ToolContext, _p: Principal, args: MessageIdArgs) -> dict[str, Any]:
    """휴지통으로 이동한다(영구 삭제 없음). IMAP 서버 반영은 P1 경로를 그대로 쓴다."""

    def _check(conn: Any) -> str:
        row = messages_repo.get_message(conn, args.message_id)
        if row is None:
            raise PermanentError(f"메일을 찾을 수 없습니다: {args.message_id}")
        folder = folders_repo.get_folder(conn, row.folder_id)
        return folder.role if folder and folder.role else ""

    role = await run_blocking(read_with_conn, tc.db, _check)
    if role == "trash":
        return {"message_id": args.message_id, "result": "already_in_trash"}
    await tc.mail_actions.move_to_trash(args.message_id)
    return {"message_id": args.message_id, "result": "moved_to_trash"}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        "create_draft",
        "새 메일 초안을 만듭니다(발송하지 않음). 주소는 user@example.com 형식만 받습니다.",
        CreateDraftArgs,
        create_draft,
    ),
    ToolSpec(
        "create_reply_draft",
        "받은 메일에 대한 회신 초안을 만듭니다. reply_all이면 내 주소·별칭은 자동 제외됩니다. "
        "원본 첨부는 기본 포함입니다(include_attachments=false로 끔).",
        CreateReplyDraftArgs,
        create_reply_draft,
    ),
    ToolSpec(
        "create_forward_draft",
        "받은 메일의 전달 초안을 만듭니다(mode: inline 또는 attach).",
        CreateForwardDraftArgs,
        create_forward_draft,
    ),
    ToolSpec(
        "update_draft",
        "작성 중(draft) 초안의 수신자·제목·본문을 바꿉니다. 이 연결로 만든 초안만 바꿀 수 "
        "있으며(사용자가 앱에서 쓴 초안은 불가), 승인 대기 중인 초안은 바꿀 수 없습니다.",
        UpdateDraftArgs,
        update_draft,
    ),
    ToolSpec(
        "delete_draft",
        "작성 중(draft) 초안을 삭제합니다. 이 연결로 만든 초안만 삭제할 수 있으며, "
        "승인 대기·발송 중인 초안은 삭제할 수 없습니다.",
        DraftIdOnlyArgs,
        delete_draft,
    ),
    ToolSpec(
        "mark_read", "메일을 읽음/안읽음으로 표시합니다(최대 100건).", MessageIdsFlagArgs, mark_read
    ),
    ToolSpec(
        "set_flag", "메일의 플래그를 켜거나 끕니다(최대 100건).", MessageIdsFlagArgs, set_flag
    ),
    ToolSpec(
        "delete_message",
        "메일을 휴지통으로 옮깁니다(영구 삭제하지 않음).",
        MessageIdArgs,
        delete_message,
    ),
]
