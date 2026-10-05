"""스코프 → 허용 도구 매핑 (DESIGN.md §6.3, S-17, M11).

범용 권한 엔진이 아니라 `{스코프: 허용 도구 집합}` 딕셔너리 하나다. 이 표를
`on_list_tools`(목록에서 숨김)와 `on_call_tool`(직접 호출 차단) 양쪽이 **각각** 참조한다
— 이중 검사(S-17). 표에 없는 도구는 어떤 스코프로도 호출할 수 없다(기본 거부).

대화형(interactive) 스코프 네 가지(read/draft/send/manage)는 P2에서 정의했다.
잡(job) 스코프 `SCOPE_JOB`(J)은 P3 Phase B에서 추가했다(`get_job_message`, `submit_auto_reply`).
대화형 스코프 집합(`INTERACTIVE_SCOPES`)과 겹치지 않고, 대화형 토큰 발급 검증
(`validate_interactive_scopes`)은 `job`을 거부한다. 서버 L3는 이 표에 더해 주체 종류
(`principal.kind`)를 먼저 분기한다(보안리뷰 L-D, server.py `tools_for_principal`).
"""

from __future__ import annotations

from collections.abc import Iterable

SCOPE_READ = "read"
SCOPE_DRAFT = "draft"
SCOPE_SEND = "send"
SCOPE_MANAGE = "manage"

INTERACTIVE_SCOPES: frozenset[str] = frozenset({SCOPE_READ, SCOPE_DRAFT, SCOPE_SEND, SCOPE_MANAGE})
# 잡 스코프(J, §6.3). 잡 토큰(mcp_server/job_tokens.py)만 이 스코프를 가진다.
SCOPE_JOB = "job"

# 기본 발급 스코프는 read+draft다. send와 manage는 UI에서 명시적으로 부여한다(§6.3, H9-4).
DEFAULT_ISSUE_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_DRAFT)

SCOPE_LABELS: dict[str, str] = {
    SCOPE_READ: "읽기",
    SCOPE_DRAFT: "초안 작성",
    SCOPE_SEND: "발송 요청",
    SCOPE_MANAGE: "메일 관리(읽음·플래그·삭제)",
}

SCOPE_TOOLS: dict[str, frozenset[str]] = {
    SCOPE_READ: frozenset(
        {
            "list_accounts",
            "list_folders",
            "list_messages",
            "search_messages",
            "get_message",
            "get_thread",
            "list_drafts",
            "get_draft_status",
            "fetch_now",
        }
    ),
    SCOPE_DRAFT: frozenset(
        {
            "create_draft",
            "create_reply_draft",
            "create_forward_draft",
            "update_draft",
            "delete_draft",
        }
    ),
    SCOPE_SEND: frozenset({"send_draft"}),
    # move_message는 P4에서 MCP에 노출한다(§6.3).
    SCOPE_MANAGE: frozenset({"mark_read", "set_flag", "delete_message"}),
    # get_job_thread·get_job_instructions·submit_summary는 이후 단계(§6.3 J 행).
    SCOPE_JOB: frozenset({"get_job_message", "submit_auto_reply"}),
}

JOB_TOOLS: frozenset[str] = SCOPE_TOOLS[SCOPE_JOB]
INTERACTIVE_TOOLS: frozenset[str] = frozenset().union(
    *(SCOPE_TOOLS[s] for s in INTERACTIVE_SCOPES)
)


def allowed_tools(scopes: Iterable[str]) -> frozenset[str]:
    """주어진 스코프들로 호출할 수 있는 도구 이름 전체."""
    result: set[str] = set()
    for scope in scopes:
        result |= SCOPE_TOOLS.get(scope, frozenset())
    return frozenset(result)


def is_tool_allowed(scopes: Iterable[str], tool_name: str) -> bool:
    return tool_name in allowed_tools(scopes)


def validate_interactive_scopes(scopes: Iterable[str]) -> list[str]:
    """발급 요청 스코프를 검증·정렬한다. 비어 있거나 모르는 값이 있으면 ValueError."""
    requested = sorted(set(scopes))
    if not requested:
        raise ValueError("스코프를 하나 이상 지정해야 합니다")
    unknown = [s for s in requested if s not in INTERACTIVE_SCOPES]
    if unknown:
        raise ValueError(f"알 수 없는 스코프입니다: {', '.join(unknown)}")
    return requested
