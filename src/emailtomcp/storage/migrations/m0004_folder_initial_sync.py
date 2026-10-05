"""폴더별 "최초 동기화 완료" 표식 (user_version=4, DESIGN.md §7.2 1단계 backfill 판정).

데카르트 31번 M-1: 예전에는 "로컬에 이 폴더의 remote_uid가 하나도 없으면" 이번에 받는 메일을
전부 backfill(과거 메일 일괄 수신)로 봤다. 그래서 받은편지함을 비운(보관함으로 이동) 뒤 받는
**새 정상 메일**도 자동회신 평가에서 조용히 빠졌다. 최초 동기화 여부를 로컬 메일 수와 분리해
폴더 단위로 영속 저장한다.

- `folders.initial_sync_done INTEGER NOT NULL DEFAULT 0` (0/1)
- 업그레이드 시 기존 폴더 처리: 이미 서버에서 받은 흔적이 있는 폴더는 최초 동기화를 마친 것으로
  본다 — ①그 폴더에 remote_uid가 있는 메일이 있거나 ②POP3 계정의 받은편지함인데 받은 UIDL
  기록이 있는 경우 ③받은편지함(INBOX)은 계정의 어느 폴더에든 remote_uid 메일이 있거나
  original_folder_id가 그 INBOX인 메일이 있는 경우(데카르트 33번 L-7).
  흔적이 없는 폴더는 0으로 남아 다음 동기화 한 번이 backfill로 처리된다
  (자동회신이 빠질 뿐, 안전한 방향).

ADD COLUMN·UPDATE만 하므로 `rebuild_table` 없이 처리한다(§5.4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # PRAGMA는 바인딩을 지원하지 않는다. table은 이 모듈의 상수에서만 온다.
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def upgrade(conn: sqlite3.Connection) -> None:
    """runner.py가 이미 걸어 둔 트랜잭션 안에서 실행된다 — 여기서 commit/rollback하지 않는다."""
    if not _table_exists(conn, "folders"):
        return
    if "initial_sync_done" not in _columns(conn, "folders"):
        conn.execute(
            "ALTER TABLE folders ADD COLUMN initial_sync_done INTEGER NOT NULL DEFAULT 0 "
            "CHECK (initial_sync_done IN (0, 1))"
        )
    if _table_exists(conn, "messages"):
        conn.execute(
            "UPDATE folders SET initial_sync_done = 1 WHERE EXISTS ("
            "SELECT 1 FROM messages m WHERE m.folder_id = folders.id "
            "AND m.remote_uid IS NOT NULL)"
        )
        # 받은편지함은 계정 단위 흔적으로도 판정한다(데카르트 33번 L-7): 업그레이드 전에 이미
        # 받은편지함을 비운 사용자는 메일이 휴지통 등으로 옮겨지며(remote_uid 유지) INBOX 자체엔
        # 흔적이 없다. 그 계정의 어느 폴더에든 remote_uid가 있는 메일이 있거나, 원래 폴더
        # (original_folder_id)가 이 INBOX인 메일이 있으면 받은편지함 최초 동기화를 마친 것으로 본다.
        conn.execute(
            "UPDATE folders SET initial_sync_done = 1 WHERE name = 'INBOX' AND ("
            "EXISTS (SELECT 1 FROM messages m WHERE m.account_id = folders.account_id "
            "AND m.remote_uid IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM messages m WHERE m.original_folder_id = folders.id))"
        )
    if _table_exists(conn, "pop3_uidl_seen") and _table_exists(conn, "accounts"):
        conn.execute(
            "UPDATE folders SET initial_sync_done = 1 WHERE name = 'INBOX' AND EXISTS ("
            "SELECT 1 FROM accounts a WHERE a.id = folders.account_id "
            "AND a.incoming_protocol = 'pop3') AND EXISTS ("
            "SELECT 1 FROM pop3_uidl_seen u WHERE u.account_id = folders.account_id)"
        )
