"""레이트리밋 (DESIGN.md §7.7, M1·S-12).

이번(P2) 범위는 **MCP send_draft — 전체** 한 줄뿐이다(시간당 20건, 하루 100건, 초과 시
`PolicyError`로 거부). 자동회신(auto_send)·잡 생성 상한은 P3에서 이 모듈에 추가한다.

상한은 메모리 카운터가 아니라 DB(`drafts`)에서 계산한다 — 앱을 재시작해도 초기화되지
않게 하기 위해서다(S-12). 경합 없이 "검사 + Outbox 전이"를 하나로 묶으려면 이 검사를
writer 스레드 안(`drafts_repo.approve_to_outbox`의 verify 콜백)에서 호출해야 한다 —
그래서 이 함수는 연결(conn)을 인자로 받는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from emailtomcp.core.errors import PolicyError
from emailtomcp.storage.repositories import drafts as drafts_repo

if TYPE_CHECKING:
    import sqlite3

MCP_SEND_HOURLY_LIMIT = 20
MCP_SEND_DAILY_LIMIT = 100


def mcp_send_usage(conn: sqlite3.Connection, now: datetime) -> tuple[int, int]:
    """(최근 1시간, 최근 24시간) MCP send_draft 발송 건수."""
    hourly = drafts_repo.count_mcp_sends_since(conn, (now - timedelta(hours=1)).isoformat())
    daily = drafts_repo.count_mcp_sends_since(conn, (now - timedelta(days=1)).isoformat())
    return hourly, daily


def check_mcp_send_limit(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    hourly_limit: int = MCP_SEND_HOURLY_LIMIT,
    daily_limit: int = MCP_SEND_DAILY_LIMIT,
) -> None:
    """이번 1건을 더 보내면 상한을 넘는지 검사한다. 넘으면 `PolicyError`.

    경계: 이미 19건이면 20건째는 허용, 이미 20건이면 21건째는 거부한다.
    """
    hourly, daily = mcp_send_usage(conn, now)
    if hourly >= hourly_limit:
        raise PolicyError(
            f"MCP 발송 상한을 넘었습니다(시간당 {hourly_limit}건). 잠시 후 다시 시도하세요."
        )
    if daily >= daily_limit:
        raise PolicyError(
            f"MCP 발송 상한을 넘었습니다(하루 {daily_limit}건). 내일 다시 시도하세요."
        )
