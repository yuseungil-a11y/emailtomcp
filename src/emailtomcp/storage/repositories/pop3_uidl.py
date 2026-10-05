"""`pop3_uidl_seen` 테이블 레포지토리 (DESIGN.md §2(b)).

로컬에서 메시지 행을 지워도(사용자가 삭제) 서버의 같은 UIDL을 다시 새 메일로
받지 않도록, 한 번 본 UIDL은 계속 기록해 둔다. `pop3_delete_after_days` 정책의
기준 날짜(`first_seen_at`)도 이 테이블에서 가져온다.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from emailtomcp.storage.db import Database


class SeenUidl(NamedTuple):
    uidl: str
    first_seen_at: str


def get_seen_uidls(conn: sqlite3.Connection, account_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT uidl FROM pop3_uidl_seen WHERE account_id = ?", (account_id,)
    ).fetchall()
    return {row["uidl"] for row in rows}


def list_seen(conn: sqlite3.Connection, account_id: int) -> list[SeenUidl]:
    rows = conn.execute(
        "SELECT uidl, first_seen_at FROM pop3_uidl_seen WHERE account_id = ? "
        "ORDER BY first_seen_at",
        (account_id,),
    ).fetchall()
    return [SeenUidl(uidl=row["uidl"], first_seen_at=row["first_seen_at"]) for row in rows]


def record_seen(db: Database, account_id: int, uidl: str, first_seen_at: str) -> None:
    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO pop3_uidl_seen (account_id, uidl, first_seen_at) "
                "VALUES (?, ?, ?)",
                (account_id, uidl, first_seen_at),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()
