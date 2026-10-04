"""테이블 재생성 헬퍼 (소크라테스 §4.6).

SQLite는 CHECK/NOT NULL 같은 제약을 `ALTER TABLE`로 바꿀 수 없다. 그래서 이후 마이그레이션에서
제약을 바꿔야 할 때는 이 헬퍼로 "새 스키마로 다시 만들고 데이터를 옮기는" 절차를 표준화한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


def rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    create_table_sql: str,
    copy_columns: list[str],
) -> None:
    """`table`을 `create_table_sql`의 새 스키마로 재생성하고 기존 데이터를 옮긴다.

    절차(하나의 트랜잭션으로 실행):
    1. 기존 테이블을 `<table>__old`로 rename한다.
    2. `create_table_sql`로 새 `<table>`을 만든다.
    3. `copy_columns`에 지정한 컬럼만 복사한다(새 CHECK 제약 위반 행이 있으면 여기서 실패한다).
    4. 기존(`__old`) 테이블을 삭제한다.
    """
    old_name = f"{table}__old"
    columns_sql = ", ".join(copy_columns)
    conn.execute("BEGIN")
    try:
        conn.execute(f"ALTER TABLE {table} RENAME TO {old_name}")
        conn.execute(create_table_sql)
        conn.execute(f"INSERT INTO {table} ({columns_sql}) SELECT {columns_sql} FROM {old_name}")
        conn.execute(f"DROP TABLE {old_name}")
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
