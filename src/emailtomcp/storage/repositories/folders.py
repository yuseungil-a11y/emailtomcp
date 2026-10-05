"""`folders` 테이블 레포지토리 (DESIGN.md §5.1, §2(b))."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emailtomcp.storage.db import Database


@dataclass(slots=True)
class Folder:
    id: int
    account_id: int
    name: str
    role: str | None
    remote_name: str | None
    uidvalidity: int | None
    last_uid: int | None
    parent_id: int | None
    # 이 폴더의 최초 동기화(backfill)를 마쳤는지(m0004, 데카르트 31번 M-1)
    initial_sync_done: bool = False


def _row_to_folder(row: sqlite3.Row) -> Folder:
    return Folder(
        id=row["id"],
        account_id=row["account_id"],
        name=row["name"],
        role=row["role"],
        remote_name=row["remote_name"],
        uidvalidity=row["uidvalidity"],
        last_uid=row["last_uid"],
        parent_id=row["parent_id"],
        initial_sync_done=bool(row["initial_sync_done"]),
    )


def get_folder(conn: sqlite3.Connection, folder_id: int) -> Folder | None:
    row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    return _row_to_folder(row) if row else None


def list_folders(conn: sqlite3.Connection, account_id: int) -> list[Folder]:
    rows = conn.execute(
        "SELECT * FROM folders WHERE account_id = ? ORDER BY id", (account_id,)
    ).fetchall()
    return [_row_to_folder(row) for row in rows]


def find_by_name(conn: sqlite3.Connection, account_id: int, name: str) -> Folder | None:
    row = conn.execute(
        "SELECT * FROM folders WHERE account_id = ? AND name = ?", (account_id, name)
    ).fetchone()
    return _row_to_folder(row) if row else None


def find_by_role(conn: sqlite3.Connection, account_id: int, role: str) -> Folder | None:
    row = conn.execute(
        "SELECT * FROM folders WHERE account_id = ? AND role = ? ORDER BY id LIMIT 1",
        (account_id, role),
    ).fetchone()
    return _row_to_folder(row) if row else None


def get_or_create_folder(
    db: Database,
    account_id: int,
    name: str,
    *,
    role: str | None = None,
    remote_name: str | None = None,
    parent_id: int | None = None,
) -> Folder:
    """이름으로 폴더를 찾고 없으면 만든다(`UNIQUE(account_id, name)`에 의존해 경합을 피한다)."""

    def _write(conn: sqlite3.Connection) -> Folder:
        existing = find_by_name(conn, account_id, name)
        if existing is not None:
            return existing
        conn.execute("BEGIN")
        try:
            cursor = conn.execute(
                "INSERT INTO folders (account_id, name, role, remote_name, parent_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_id, name, role, remote_name, parent_id),
            )
        except sqlite3.IntegrityError:
            # 경합으로 다른 쓰기가 먼저 만들었을 수 있다 — 조회로 복구한다.
            conn.rollback()
            found = find_by_name(conn, account_id, name)
            if found is None:
                raise
            return found
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            assert cursor.lastrowid is not None
            return Folder(
                id=cursor.lastrowid,
                account_id=account_id,
                name=name,
                role=role,
                remote_name=remote_name,
                uidvalidity=None,
                last_uid=None,
                parent_id=parent_id,
            )

    return db.writer.submit(_write).result()


def mark_initial_sync_done(db: Database, folder_id: int) -> None:
    """폴더의 최초 동기화(backfill) 완료를 영속 기록한다. 이후 받는 메일은 backfill이 아니다."""

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE folders SET initial_sync_done = 1 WHERE id = ? AND initial_sync_done = 0",
                (folder_id,),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


def update_uid_state(
    db: Database, folder_id: int, *, uidvalidity: int | None, last_uid: int | None
) -> None:
    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE folders SET uidvalidity = ?, last_uid = ? WHERE id = ?",
                (uidvalidity, last_uid, folder_id),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()
