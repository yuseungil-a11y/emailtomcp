"""마이그레이션 러너.

`PRAGMA user_version`을 기준으로 순차 적용한다. 각 마이그레이션은 하나의 트랜잭션으로
실행한다. 앱이 아는 최대 버전보다 DB의 user_version이 크면(= 이후 버전 앱으로 DB를 만든 뒤
이전 버전 앱을 실행한 경우) 실행을 거부한다(소크라테스 §4.6).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from emailtomcp.core.errors import PermanentError
from emailtomcp.storage.migrations import (
    m0001_init,
    m0002_parse_limited,
    m0003_autoreply_toggle,
    m0004_folder_initial_sync,
)

if TYPE_CHECKING:
    import sqlite3

MigrationFn = Callable[["sqlite3.Connection"], None]

# 버전 번호 -> 그 버전으로 올리는 upgrade 함수.
MIGRATIONS: dict[int, MigrationFn] = {
    1: m0001_init.upgrade,
    2: m0002_parse_limited.upgrade,
    3: m0003_autoreply_toggle.upgrade,
    4: m0004_folder_initial_sync.upgrade,
}

CURRENT_SCHEMA_VERSION = max(MIGRATIONS)


def get_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA는 플레이스홀더 바인딩을 지원하지 않으므로 int()로 강제 변환해 안전하게 꽂는다.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def run_migrations(conn: sqlite3.Connection) -> int:
    """미적용 마이그레이션을 순서대로 적용하고 최종 user_version을 반환한다."""
    current = get_user_version(conn)
    if current > CURRENT_SCHEMA_VERSION:
        raise PermanentError(
            f"DB user_version({current})이 이 앱이 아는 최대 버전({CURRENT_SCHEMA_VERSION})보다 "
            "큽니다. 더 최신 버전의 앱으로 실행하세요."
        )
    for version in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
        migrate_fn = MIGRATIONS[version]
        conn.execute("BEGIN")
        try:
            migrate_fn(conn)
            set_user_version(conn, version)
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
    return get_user_version(conn)
