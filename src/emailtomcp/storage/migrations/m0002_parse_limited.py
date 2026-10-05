"""`messages.parse_limited` 컬럼 추가 (user_version=2).

파싱 제한 모드(§8.2 L5 MIME 상한 초과)로 저장된 메일인지 UI 그리드에 배지로
보여주기 위한 컬럼이다. CHECK/NOT NULL 제약 변경이 아니라 단순 컬럼 추가라서
`rebuild_table` 헬퍼 없이 `ALTER TABLE ... ADD COLUMN`으로 충분하다(소크라테스 §4.6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    """runner.py가 이미 걸어 둔 트랜잭션 안에서 실행된다 — 여기서 commit/rollback하지 않는다."""
    conn.execute("ALTER TABLE messages ADD COLUMN parse_limited INTEGER NOT NULL DEFAULT 0")
