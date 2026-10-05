"""원자적 상태 전이 헬퍼 (소크라테스 §3.2, P0 체크리스트).

모든 상태 전이는 `UPDATE ... SET status=? WHERE id=? AND status IN (...)` 형태로 하고
rowcount로 성공 여부를 확인한다. messages/drafts/auto_reply_jobs 세 테이블의 상태 컬럼에
동일한 방식을 적용한다. 재시작 시 'running'을 'queued'로 되돌리는 로직과, 실행 중인
러너가 'done'을 쓰는 로직이 경쟁해도 이 함수를 쓰면 한쪽만 성공한다.

**approved_by 초기화 규칙(DESIGN.md §5.3, §7.9 M-B, 불변식 I8)**: `drafts`를 `draft`로
되돌리는 모든 전이에서, `approved_by='policy_auto_send'`(자동회신 정책 자동발송)였던 행은
같은 UPDATE 문 안에서 `approved_by`·`approved_at`·`outbox_expires_at`을 NULL로 만든다.
호출자가 고르는 옵션이 아니라 이 함수가 항상 한다 — 그래야 끄기·긴급정지·설정 변경 드레인·
G8 거부·만료·[편집] 중 어느 경로로 draft가 되더라도 "자동 승인"이 draft에 남지 않는다
(I8: policy_auto_send 행은 draft·pending_approval에 없다). 다른 approved_by 값(`ui`,
`policy_allowlist`, `user_send`)의 처리는 바꾸지 않는다(P2 MCP 레이트리밋 집계 근거).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from emailtomcp.core.models import ApprovedBy, DraftStatus

if TYPE_CHECKING:
    import sqlite3

# 임의 테이블명을 SQL에 그대로 꽂지 않기 위한 허용 목록(테이블명 -> 기본 상태 컬럼명).
_DEFAULT_STATUS_COLUMNS: dict[str, str] = {
    "messages": "auto_reply_status",
    "drafts": "status",
    "auto_reply_jobs": "status",
}

# I8 초기화 SET 절. SQLite의 UPDATE는 SET 식을 모두 "갱신 전 행" 기준으로 평가하므로,
# 세 CASE가 모두 원래의 approved_by 값을 본다(앞의 SET이 approved_by를 NULL로 바꿔도 뒤 CASE에
# 영향이 없다).
_DRAFT_RESET_SQL = (
    ", approved_by = CASE WHEN approved_by = ? THEN NULL ELSE approved_by END"
    ", approved_at = CASE WHEN approved_by = ? THEN NULL ELSE approved_at END"
    ", outbox_expires_at = CASE WHEN approved_by = ? THEN NULL ELSE outbox_expires_at END"
)


def transition(
    conn: sqlite3.Connection,
    table: str,
    row_id: int,
    from_states: Sequence[str],
    to_state: str,
    *,
    status_column: str | None = None,
    id_column: str = "id",
    commit: bool = True,
) -> bool:
    """`row_id`의 상태가 `from_states` 중 하나일 때만 `to_state`로 원자적으로 바꾼다.

    Args:
        commit: True(기본, 기존 동작)이면 전이 직후 커밋한다. False면 커밋하지 않는다 —
            여러 전이와 설정 변경을 **하나의 트랜잭션**으로 묶어야 하는 호출자(자동회신
            끄기 드레인 `storage/repositories/autoreply.py`)가 마지막에 직접 커밋한다.

    Returns:
        실제로 전이가 일어났으면 True(rowcount == 1), 이미 다른 상태였으면 False.
    """
    if table not in _DEFAULT_STATUS_COLUMNS:
        raise ValueError(f"transition()이 허용하지 않는 테이블입니다: {table}")
    if not from_states:
        raise ValueError("from_states는 비어 있을 수 없습니다")

    column = status_column or _DEFAULT_STATUS_COLUMNS[table]
    placeholders = ",".join("?" for _ in from_states)
    set_sql = f"{column} = ?"
    set_params: list[object] = [to_state]
    if table == "drafts" and column == "status" and to_state == DraftStatus.DRAFT:
        # I8 / M-B: policy_auto_send 승인은 draft로 돌아가는 순간 무효가 된다.
        set_sql += _DRAFT_RESET_SQL
        auto = ApprovedBy.POLICY_AUTO_SEND.value
        set_params.extend((auto, auto, auto))
    # table/column은 위 허용 목록에서만 오므로 f-string을 써도 인젝션 위험이 없다.
    sql = f"UPDATE {table} SET {set_sql} WHERE {id_column} = ? AND {column} IN ({placeholders})"
    cursor = conn.execute(sql, (*set_params, row_id, *from_states))
    if commit:
        conn.commit()
    return cursor.rowcount == 1
