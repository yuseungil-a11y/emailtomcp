"""send 스코프 도구 — `send_draft` (DESIGN.md §6.2, §6.3, §7.7, H9).

처리 순서
1. `min_version_floor` 미달이면 거부(§6.1, §14.4).
2. 이 연결(토큰)이 만든 MCP 초안(`origin='mcp'`)인지 확인한다 — 사용자가 앱에서 쓴
   초안은 발송 요청 대상이 아니다(보안검토 H-1, `tools_draft.require_mcp_owned`).
3. 초안이 `draft` 상태인지 확인(이미 승인 대기면 현재 상태만 알려 준다).
4. 발송 모드(`mcp.send_mode`, 기본 confirm):
   - `disabled`: 거부(초안만 만들 수 있다).
   - `allowlist`: To/CC/BCC **전원**이 허용 목록(`mcp.allowlist`, 주소 또는 도메인)에
     있고, **첨부가 하나도 없을 때만**(종류 무관 — 회신·전달·새 초안 모두, 보안검토 M-2)
     바로 Outbox로 보낸다. 그 밖에는 confirm.
     요청 시점의 판정은 "빠른 길"을 고르는 용도일 뿐이다. 실제 `draft → outbox` 전이는
     writer 트랜잭션 안에서 **그 트랜잭션이 읽은 초안 행으로 발송 모드·허용 목록·첨부를
     다시 판정**하고, 그 사이 내용이 바뀌어 조건이 깨졌으면 `PolicyError`로 거부한다
     (보안검토 H-2: 판정 직후 `update_draft`로 외부 BCC를 끼워 넣는 TOCTOU 차단).
   - `confirm`: 스냅샷 해시를 저장하고 `pending_approval`로 바꾼 뒤 UI 승인을 최대
     120초 기다린다. 시간이 지나면 "승인 대기 중"을 돌려주고(블로킹하지 않음) 초안은
     승인 대기함에 남는다. 승인 요청 폭주를 막는 상한(토큰당 동시 대기 3건, 분당 요청
     5건, 거부 직후 재요청 60초 쿨다운)은 `ApprovalService.request`가 writer 안에서
     검사한다(보안검토 M-1).
5. 레이트리밋(시간당 20건, 하루 100건)은 세 모드 모두에 적용한다. 요청 시점에 먼저
   확인하고, 실제 Outbox 전이 시점(writer 트랜잭션 안)에 다시 확인한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import Field

from emailtomcp.core.errors import PermanentError, PolicyError
from emailtomcp.core.models import DraftKind, DraftStatus
from emailtomcp.mail.addressing import addr_domain_norm, normalize_addr, normalize_domain
from emailtomcp.mcp_server.approval import Decision
from emailtomcp.mcp_server.tool_base import StrictArgs, ToolContext, ToolSpec, run_blocking
from emailtomcp.mcp_server.tools_draft import require_mcp_owned
from emailtomcp.mcp_server.tools_read import read_with_conn
from emailtomcp.rules import rate_limit
from emailtomcp.storage.repositories import drafts as drafts_repo

if TYPE_CHECKING:
    from emailtomcp.mcp_server.auth import Principal
    from emailtomcp.storage.repositories.drafts import DraftRow

SEND_MODE_DISABLED = "disabled"
SEND_MODE_CONFIRM = "confirm"
SEND_MODE_ALLOWLIST = "allowlist"
SEND_MODES = (SEND_MODE_DISABLED, SEND_MODE_CONFIRM, SEND_MODE_ALLOWLIST)
DEFAULT_SEND_MODE = SEND_MODE_CONFIRM

SETTING_SEND_MODE = "mcp.send_mode"
SETTING_ALLOWLIST = "mcp.allowlist"


class SendDraftArgs(StrictArgs):
    draft_id: int = Field(ge=1)


def resolve_send_mode(value: Any) -> str:
    return value if value in SEND_MODES else DEFAULT_SEND_MODE


def allowlist_permits(draft: DraftRow, entries: list[str]) -> bool:
    """To/CC/BCC **모든** 수신자가 허용 목록(주소 또는 도메인)에 있는지 본다(H9-2).

    - `user@example.com` 형태는 주소 정확 일치.
    - `example.com` 또는 `@example.com` 형태는 도메인 정확 일치(하위 도메인은 포함하지 않음).
    - 수신자가 하나도 없거나, 주소를 해석할 수 없으면 False(=confirm으로 처리).
    """
    addrs: set[str] = set()
    domains: set[str] = set()
    for raw in entries:
        entry = str(raw).strip().lower()
        if not entry:
            continue
        if "@" in entry and not entry.startswith("@"):
            addrs.add(normalize_addr(entry))
        else:
            domains.add(normalize_domain(entry.lstrip("@")))
    recipients = [*draft.to_addrs, *draft.cc_addrs, *draft.bcc_addrs]
    if not recipients:
        return False
    for recipient in recipients:
        domain = addr_domain_norm(recipient)
        if not domain:
            return False
        if normalize_addr(recipient) not in addrs and domain not in domains:
            return False
    return True


def is_forward_with_attachments(draft: DraftRow) -> bool:
    """첨부가 포함된 전달인지(H9-2: 이런 초안은 allowlist 모드에서도 항상 confirm)."""
    if draft.kind == DraftKind.FORWARD_ATTACH:
        return True
    return draft.kind == DraftKind.FORWARD_INLINE and bool(draft.attachments)


def allowlist_requires_confirm(draft: DraftRow) -> bool:
    """allowlist 모드에서도 confirm을 강제해야 하는 초안인지(H9-2 + 보안검토 M-2).

    첨부 전달뿐 아니라 **첨부가 하나라도 있는 모든 초안**(원본 첨부를 기본 포함하는 회신,
    사용자가 로컬 파일을 붙인 초안 등)은 사람이 확인해야 한다.
    """
    return is_forward_with_attachments(draft) or bool(draft.attachments)


def allowlist_violation(draft: DraftRow, mode: str, allowlist: list[str]) -> str | None:
    """allowlist 정책으로 승인 없이 보낼 수 없는 이유. 보낼 수 있으면 None."""
    if mode != SEND_MODE_ALLOWLIST:
        return "발송 모드가 허용 목록(allowlist) 모드가 아닙니다"
    if allowlist_requires_confirm(draft):
        return "첨부가 있는 초안은 사용자 승인이 필요합니다"
    if not allowlist_permits(draft, allowlist):
        return "허용 목록에 없는 수신자가 있습니다"
    return None


def _load_settings(tc: ToolContext) -> tuple[str, list[str]]:
    mode = resolve_send_mode(tc.get_setting(SETTING_SEND_MODE, DEFAULT_SEND_MODE))
    raw_list = tc.get_setting(SETTING_ALLOWLIST, [])
    allowlist = [str(x) for x in raw_list] if isinstance(raw_list, list) else []
    return mode, allowlist


def _precheck(
    tc: ToolContext, principal: Principal, draft_id: int
) -> tuple[DraftRow, str, list[str]]:
    draft = read_with_conn(tc.db, lambda conn: drafts_repo.get_draft(conn, draft_id))
    if draft is None:
        raise PermanentError(f"초안을 찾을 수 없습니다: {draft_id}")
    require_mcp_owned(draft, principal, "발송 요청")
    mode, allowlist = _load_settings(tc)
    if draft.status == DraftStatus.DRAFT:
        if not (draft.to_addrs or draft.cc_addrs or draft.bcc_addrs):
            raise PermanentError("수신자가 없는 초안은 보낼 수 없습니다")
        if mode == SEND_MODE_DISABLED:
            raise PolicyError(
                "MCP 발송이 꺼져 있습니다(설정 > MCP > 발송 모드). 초안만 만들 수 있습니다."
            )
        # 상한 초과 상태에서 승인 요청이 쌓이지 않도록 요청 시점에 먼저 확인한다.
        read_with_conn(tc.db, lambda conn: rate_limit.check_mcp_send_limit(conn, tc.clock.now()))
    return draft, mode, allowlist


_DECISION_RESULTS: dict[str, tuple[str, str]] = {
    Decision.APPROVED: ("outbox", "사용자가 승인했습니다. 보낼편지함에서 곧 발송됩니다."),
    Decision.REJECTED: (
        "draft",
        "사용자가 발송을 거부했습니다. 초안은 작성 중 상태로 남아 있습니다.",
    ),
    Decision.EDIT: (
        "draft",
        "사용자가 직접 수정한 뒤 보내기로 했습니다. 이 초안은 사용자가 처리합니다.",
    ),
    Decision.EXPIRED: ("draft", "승인 대기 시간이 지나 초안으로 돌아갔습니다."),
    Decision.INVALIDATED: (
        "draft",
        "승인 요청 이후 초안 내용이 바뀌어 승인이 무효 처리되었습니다.",
    ),
}


async def send_draft(tc: ToolContext, p: Principal, args: SendDraftArgs) -> dict[str, Any]:
    if not await run_blocking(tc.send_allowed_by_floor):
        raise PolicyError(
            "필수 업데이트가 필요해 MCP 발송이 정지되었습니다(min_version_floor 미달)."
        )
    draft, mode, allowlist = await run_blocking(_precheck, tc, p, args.draft_id)

    if draft.status == DraftStatus.PENDING_APPROVAL:
        return {
            "draft_id": draft.id,
            "status": "pending_approval",
            "message": "이미 사용자 승인을 기다리는 중입니다. get_draft_status로 확인하세요.",
        }
    if draft.status != DraftStatus.DRAFT:
        raise PolicyError(f"발송을 요청할 수 없는 상태입니다(현재: {draft.status})")

    if allowlist_violation(draft, mode, allowlist) is None:
        # 위 판정은 읽기 스냅샷 기준이다. 실제 전이는 writer 안에서 다시 판정한다(H-2).
        def _policy_check(current: DraftRow) -> None:
            require_mcp_owned(current, p, "발송 요청")
            cur_mode, cur_allowlist = _load_settings(tc)
            reason = allowlist_violation(current, cur_mode, cur_allowlist)
            if reason is not None:
                raise PolicyError(
                    f"발송 판정 이후 초안 내용이나 설정이 바뀌어 승인 없이 보낼 수 없습니다"
                    f"({reason}). 초안은 작성 중 상태로 남아 있습니다 — 다시 send_draft를 "
                    "호출하면 사용자 승인 절차로 진행됩니다."
                )

        if not await run_blocking(tc.approval.send_by_policy, draft.id, _policy_check):
            raise PolicyError("초안 상태가 바뀌어 발송하지 못했습니다")
        return {
            "draft_id": draft.id,
            "status": "outbox",
            "message": "모든 수신자가 허용 목록에 있어 승인 없이 보낼편지함에 넣었습니다.",
        }

    # confirm(기본) — 결정을 놓치지 않도록 대기 등록을 먼저 하고 요청한다.
    future = tc.approval.register_waiter(draft.id)
    try:
        # 소유권은 writer 안에서도 다시 확인한다(사전 확인 이후 사용자가 넘겨받았을 수
        # 있다, 보안검토 N-1).
        await run_blocking(
            tc.approval.request,
            draft.id,
            lambda current: require_mcp_owned(current, p, "발송 요청"),
        )
        decision = await tc.approval.wait(future, tc.approval_wait_seconds)
    finally:
        tc.approval.unregister_waiter(draft.id, future)

    if decision is None:
        return {
            "draft_id": draft.id,
            "status": "pending_approval",
            "message": (
                "사용자 승인을 기다리는 중입니다(앱의 '승인 대기' 함에 있습니다). "
                "나중에 get_draft_status로 결과를 확인하세요."
            ),
        }
    status, message = _DECISION_RESULTS.get(decision, ("unknown", f"결과: {decision}"))
    return {"draft_id": draft.id, "status": status, "decision": decision, "message": message}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        "send_draft",
        "이 연결로 만든 초안의 발송을 요청합니다(사용자가 앱에서 쓴 초안은 불가). "
        "기본 설정에서는 사용자가 앱에서 직접 승인해야 발송되며, "
        "최대 120초 기다린 뒤 응답이 없으면 'pending_approval'을 돌려줍니다.",
        SendDraftArgs,
        send_draft,
    ),
]
