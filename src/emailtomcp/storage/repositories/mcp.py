"""`mcp_tokens`/`mcp_audit` 테이블 레포지토리 (DESIGN.md §5.1, §6.5, §8.6).

- `mcp_tokens`에는 토큰 평문을 저장하지 않는다. sha256 해시만 둔다(H4). proxy 토큰의
  평문은 keyring에만 있다 — 그 처리는 호출자(`mcp_server.auth`)의 몫이다.
- `mcp_audit`의 인자는 호출자가 이미 마스킹한 JSON만 받는다(M6). 이 모듈은 마스킹을
  하지 않는다.

raw sqlite3는 storage/ 밖에서 쓰지 않는다는 원칙(§4.3)에 따라 MCP 계층의 SQL은
전부 여기 둔다. 쓰기는 모두 `Database.writer`를 거친다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emailtomcp.storage.db import Database


@dataclass(slots=True)
class McpTokenRow:
    id: int
    label: str
    kind: str  # "proxy" | "http"
    scopes: list[str]
    token_sha256: str
    created_at: str
    last_used_at: str | None
    expires_at: str | None
    revoked_at: str | None


@dataclass(slots=True)
class McpAuditRow:
    id: int
    ts: str
    principal: str
    token_id: int | None
    token_hash8: str | None
    job_id: int | None
    tool: str | None
    args_masked_json: str | None
    result: str
    http_status: int | None
    detail: str | None


def _row_to_token(row: sqlite3.Row) -> McpTokenRow:
    return McpTokenRow(
        id=row["id"],
        label=row["label"],
        kind=row["kind"],
        scopes=json.loads(row["scopes_json"]) if row["scopes_json"] else [],
        token_sha256=row["token_sha256"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


def _row_to_audit(row: sqlite3.Row) -> McpAuditRow:
    return McpAuditRow(
        id=row["id"],
        ts=row["ts"],
        principal=row["principal"],
        token_id=row["token_id"],
        token_hash8=row["token_hash8"],
        job_id=row["job_id"],
        tool=row["tool"],
        args_masked_json=row["args_masked_json"],
        result=row["result"],
        http_status=row["http_status"],
        detail=row["detail"],
    )


# ---------------------------------------------------------------- 토큰


def insert_token(
    db: Database,
    *,
    label: str,
    kind: str,
    scopes: list[str],
    token_sha256: str,
    created_at: str,
    expires_at: str | None,
) -> int:
    def _write(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN")
        try:
            cursor = conn.execute(
                "INSERT INTO mcp_tokens (label, kind, scopes_json, token_sha256, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (label, kind, json.dumps(scopes), token_sha256, created_at, expires_at),
            )
            assert cursor.lastrowid is not None
            token_id = cursor.lastrowid
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return token_id

    return db.writer.submit(_write).result()


def get_token(conn: sqlite3.Connection, token_id: int) -> McpTokenRow | None:
    row = conn.execute("SELECT * FROM mcp_tokens WHERE id = ?", (token_id,)).fetchone()
    return _row_to_token(row) if row else None


def find_token_by_hash(conn: sqlite3.Connection, token_sha256: str) -> McpTokenRow | None:
    """해시로 토큰 행을 찾는다. 최종 일치 판정(`compare_digest`)은 호출자가 한다."""
    row = conn.execute(
        "SELECT * FROM mcp_tokens WHERE token_sha256 = ?", (token_sha256,)
    ).fetchone()
    return _row_to_token(row) if row else None


def list_tokens(conn: sqlite3.Connection) -> list[McpTokenRow]:
    rows = conn.execute("SELECT * FROM mcp_tokens ORDER BY id DESC").fetchall()
    return [_row_to_token(row) for row in rows]


def list_active_proxy_tokens(conn: sqlite3.Connection, label: str) -> list[McpTokenRow]:
    rows = conn.execute(
        "SELECT * FROM mcp_tokens WHERE kind = 'proxy' AND label = ? AND revoked_at IS NULL",
        (label,),
    ).fetchall()
    return [_row_to_token(row) for row in rows]


def revoke_token(db: Database, token_id: int, revoked_at: str) -> bool:
    """폐기 시각을 기록한다. 이미 폐기된 토큰이면 False."""

    def _write(conn: sqlite3.Connection) -> bool:
        conn.execute("BEGIN")
        try:
            cursor = conn.execute(
                "UPDATE mcp_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (revoked_at, token_id),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return cursor.rowcount == 1

    return db.writer.submit(_write).result()


def touch_token(db: Database, token_id: int, used_at: str) -> None:
    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute("UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?", (used_at, token_id))
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


# ---------------------------------------------------------------- 감사


def insert_audit(
    db: Database,
    *,
    ts: str,
    principal: str,
    token_id: int | None,
    token_hash8: str | None,
    job_id: int | None,
    tool: str | None,
    args_masked_json: str | None,
    result: str,
    http_status: int | None,
    detail: str | None,
) -> int:
    def _write(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN")
        try:
            cursor = conn.execute(
                "INSERT INTO mcp_audit (ts, principal, token_id, token_hash8, job_id, tool, "
                "args_masked_json, result, http_status, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    principal,
                    token_id,
                    token_hash8,
                    job_id,
                    tool,
                    args_masked_json,
                    result,
                    http_status,
                    detail,
                ),
            )
            assert cursor.lastrowid is not None
            audit_id = cursor.lastrowid
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return audit_id

    return db.writer.submit(_write).result()


def list_audit(conn: sqlite3.Connection, *, limit: int = 200) -> list[McpAuditRow]:
    rows = conn.execute("SELECT * FROM mcp_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_audit(row) for row in rows]


def count_audit(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM mcp_audit").fetchone()
    return int(row[0])


def purge_audit_before(db: Database, before_iso: str) -> int:
    """보존 기간(기본 90일, §5.6)이 지난 감사 기록을 지운다."""

    def _write(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN")
        try:
            cursor = conn.execute("DELETE FROM mcp_audit WHERE ts < ?", (before_iso,))
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return cursor.rowcount

    return db.writer.submit(_write).result()
