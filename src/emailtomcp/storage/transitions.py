"""원자적 상태 전이 헬퍼 (소크라테스 §3.2, P0 체크리스트).

모든 상태 전이는 `UPDATE ... SET status=? WHERE id=? AND status IN (...)` 형태로 하고
rowcount로 성공 여부를 확인한다. messages/drafts/auto_reply_jobs 세 테이블의 상태 컬럼에
동일한 방식을 적용한다. 재시작 시 'running'을 'queued'로 되돌리는 로직과, 실행 중인
러너가 'done'을 쓰는 로직이 경쟁해도 이 함수를 쓰면 한쪽만 성공한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

# 임의 테이블명을 SQL에 그대로 꽂지 않기 위한 허용 목록(테이블명 -> 기본 상태 컬럼명).
_DEFAULT_STATUS_COLUMNS: dict[str, str] = {
    "messages": "auto_reply_status",
    "drafts": "status",
    "auto_reply_jobs": "status",
}


def transition(
    conn: sqlite3.Connection,
    table: str,
    row_id: int,
    from_states: Sequence[str],
    to_state: str,
    *,
    status_column: str | None = None,
    id_column: str = "id",
) -> bool:
    """`row_id`의 상태가 `from_states` 중 하나일 때만 `to_state`로 원자적으로 바꾼다.

    Returns:
        실제로 전이가 일어났으면 True(rowcount == 1), 이미 다른 상태였으면 False.
    """
    if table not in _DEFAULT_STATUS_COLUMNS:
        raise ValueError(f"transition()이 허용하지 않는 테이블입니다: {table}")
    if not from_states:
        raise ValueError("from_states는 비어 있을 수 없습니다")

    column = status_column or _DEFAULT_STATUS_COLUMNS[table]
    placeholders = ",".join("?" for _ in from_states)
    # table/column은 위 허용 목록에서만 오므로 f-string을 써도 인젝션 위험이 없다.
    sql = f"UPDATE {table} SET {column} = ? WHERE {id_column} = ? AND {column} IN ({placeholders})"
    cursor = conn.execute(sql, (to_state, row_id, *from_states))
    conn.commit()
    return cursor.rowcount == 1
