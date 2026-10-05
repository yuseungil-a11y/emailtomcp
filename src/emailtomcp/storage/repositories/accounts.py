"""`accounts` 테이블 레포지토리 (DESIGN.md §5.1).

비밀번호/OAuth 토큰은 이 테이블에 없다 — keyring에만 있다(`secrets.keyring_store`).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emailtomcp.storage.db import Database


@dataclass(slots=True)
class Account:
    id: int
    display_name: str
    email_address: str
    aliases: list[str]
    sender_name: str | None
    provider_preset: str | None
    auth_method: str
    incoming_protocol: str
    in_host: str
    in_port: int
    in_security: str
    in_username: str
    out_host: str
    out_port: int
    out_security: str
    out_username: str | None
    out_auth_same_as_in: bool
    pop3_leave_on_server: bool
    pop3_delete_after_days: int | None
    poll_interval_sec: int
    signature: str | None
    trusted_authserv_id: str | None
    auto_reply_enabled: bool
    enabled: bool
    # 수신 경계 MTA의 Received `by` 호스트 패턴(§7.6 A1·A2, m0003). 사용자 수동 입력만.
    trusted_boundary_by: str | None = None

    @property
    def effective_out_username(self) -> str:
        """SMTP 인증에 쓸 사용자명. `out_auth_same_as_in`이면 수신 계정명을 쓴다."""
        if self.out_auth_same_as_in or not self.out_username:
            return self.in_username
        return self.out_username


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        display_name=row["display_name"],
        email_address=row["email_address"],
        aliases=json.loads(row["aliases_json"]) if row["aliases_json"] else [],
        sender_name=row["sender_name"],
        provider_preset=row["provider_preset"],
        auth_method=row["auth_method"],
        incoming_protocol=row["incoming_protocol"],
        in_host=row["in_host"],
        in_port=row["in_port"],
        in_security=row["in_security"],
        in_username=row["in_username"],
        out_host=row["out_host"],
        out_port=row["out_port"],
        out_security=row["out_security"],
        out_username=row["out_username"],
        out_auth_same_as_in=bool(row["out_auth_same_as_in"]),
        pop3_leave_on_server=bool(row["pop3_leave_on_server"]),
        pop3_delete_after_days=row["pop3_delete_after_days"],
        poll_interval_sec=row["poll_interval_sec"],
        signature=row["signature"],
        trusted_authserv_id=row["trusted_authserv_id"],
        auto_reply_enabled=bool(row["auto_reply_enabled"]),
        enabled=bool(row["enabled"]),
        trusted_boundary_by=(
            row["trusted_boundary_by"] if "trusted_boundary_by" in row.keys() else None  # noqa: SIM118
        ),
    )


def get_account(conn: sqlite3.Connection, account_id: int) -> Account | None:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return _row_to_account(row) if row else None


def list_enabled_accounts(conn: sqlite3.Connection) -> list[Account]:
    rows = conn.execute("SELECT * FROM accounts WHERE enabled = 1 ORDER BY id").fetchall()
    return [_row_to_account(row) for row in rows]


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [_row_to_account(row) for row in rows]


def insert_account(
    db: Database,
    *,
    display_name: str,
    email_address: str,
    incoming_protocol: str,
    in_host: str,
    in_port: int,
    in_security: str,
    in_username: str,
    out_host: str,
    out_port: int,
    out_security: str,
    aliases: list[str] | None = None,
    sender_name: str | None = None,
    provider_preset: str | None = None,
    auth_method: str = "password",
    out_username: str | None = None,
    out_auth_same_as_in: bool = True,
    pop3_leave_on_server: bool = True,
    pop3_delete_after_days: int | None = None,
    poll_interval_sec: int = 300,
    signature: str | None = None,
    trusted_authserv_id: str | None = None,
    auto_reply_enabled: bool = False,
    enabled: bool = True,
    trusted_boundary_by: str | None = None,
) -> int:
    """계정 하나를 만든다(UI 화면이 생기기 전까지 테스트/초기 설정용 창구)."""
    now = datetime.now(UTC).isoformat()

    def _write(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN")
        try:
            cursor = conn.execute(
                """
                INSERT INTO accounts (
                    display_name, email_address, aliases_json, sender_name, provider_preset,
                    auth_method, incoming_protocol, in_host, in_port, in_security, in_username,
                    out_host, out_port, out_security, out_username, out_auth_same_as_in,
                    pop3_leave_on_server, pop3_delete_after_days, poll_interval_sec, signature,
                    trusted_authserv_id, auto_reply_enabled, enabled, created_at, updated_at,
                    trusted_boundary_by
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    display_name,
                    email_address,
                    json.dumps(aliases or [], ensure_ascii=False),
                    sender_name,
                    provider_preset,
                    auth_method,
                    incoming_protocol,
                    in_host,
                    in_port,
                    in_security,
                    in_username,
                    out_host,
                    out_port,
                    out_security,
                    out_username,
                    int(out_auth_same_as_in),
                    int(pop3_leave_on_server),
                    pop3_delete_after_days,
                    poll_interval_sec,
                    signature,
                    trusted_authserv_id,
                    int(auto_reply_enabled),
                    int(enabled),
                    now,
                    now,
                    trusted_boundary_by,
                ),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    return db.writer.submit(_write).result()


_UPDATABLE_FIELDS: tuple[str, ...] = (
    "display_name",
    "sender_name",
    "provider_preset",
    "incoming_protocol",
    "in_host",
    "in_port",
    "in_security",
    "in_username",
    "out_host",
    "out_port",
    "out_security",
    "out_username",
    "out_auth_same_as_in",
    "pop3_leave_on_server",
    "pop3_delete_after_days",
    "poll_interval_sec",
    "signature",
    "trusted_authserv_id",
    "trusted_boundary_by",
    "auto_reply_enabled",
    "enabled",
)

# 자동발송 신뢰 근거가 되는 컬럼(§7.9 M-A). 이 값을 바꾸는 저장 트랜잭션은 같은 트랜잭션에서
# 그 계정의 policy 자동발송 outbox를 draft로 되돌린다(2차 방어 — Phase B에는 그런 행이 없어
# 실제로는 항상 0건이지만, Phase C에서 G7이 열려도 이 경로가 빠지지 않게 지금 붙여 둔다).
_TRUST_FIELDS: frozenset[str] = frozenset(
    {
        "trusted_authserv_id",
        "trusted_boundary_by",
        "auto_reply_enabled",
        "in_security",
        "out_security",
        "signature",
        "enabled",
        # Spinoza 34번 L-2: §7.6③ ar_capability가 수신 호스트로 프리셋을 찾고, 송신 경로도
        # 발송 신뢰 근거라 함께 드레인 대상이다.
        "in_host",
        "incoming_protocol",
        "out_host",
        "out_username",
    }
)


def _db_value(value: object) -> object:
    """저장 시 SQLite에 들어가는 형태로 맞춘다(bool → 0/1). 변경 여부 비교용."""
    if isinstance(value, bool):
        return int(value)
    return value


def update_account(db: Database, account_id: int, **fields: object) -> bool:
    """허용된 컬럼만 갱신한다(일반/수신/송신/자동회신 탭 공용, §11.3).

    비밀번호는 여기서 다루지 않는다 — keyring에만 저장한다(`secrets.keyring_store`).

    신뢰 근거 컬럼(`_TRUST_FIELDS`)은 **값이 실제로 바뀐 경우에만** outbox를 드레인한다
    (Spinoza 34번 L-2 — 계정창 전체 저장마다 대기 outbox가 초안으로 돌아가는 가용성 문제 방지).
    비교는 같은 writer 트랜잭션 안에서 UPDATE 직전 값과 한다.
    """
    columns = [k for k in fields if k in _UPDATABLE_FIELDS]
    if not columns:
        return False
    assignments = ", ".join(f"{col} = ?" for col in columns)
    values = [fields[col] for col in columns]

    trust_columns = [col for col in columns if col in _TRUST_FIELDS]

    def _write(conn: sqlite3.Connection) -> bool:
        conn.execute("BEGIN")
        try:
            drains = False
            if trust_columns:
                # 컬럼명은 _UPDATABLE_FIELDS 화이트리스트 안의 값뿐이라 f-string이 안전하다.
                before = conn.execute(
                    f"SELECT {', '.join(trust_columns)} FROM accounts WHERE id = ?",
                    (account_id,),
                ).fetchone()
                drains = before is not None and any(
                    before[col] != _db_value(fields[col]) for col in trust_columns
                )
            cursor = conn.execute(
                f"UPDATE accounts SET {assignments}, updated_at = ? WHERE id = ?",
                (*values, datetime.now(UTC).isoformat(), account_id),
            )
            if drains:
                # 순환 import를 피하려고 함수 안에서 가져온다(autoreply 모듈은 accounts를 모른다).
                from emailtomcp.storage.repositories.autoreply import drain_autosend_outbox

                drain_autosend_outbox(
                    conn, account_id=account_id, rule_id=None, reason="account_settings_changed"
                )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return cursor.rowcount == 1

    return db.writer.submit(_write).result()


def set_aliases(db: Database, account_id: int, aliases: list[str]) -> None:
    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE accounts SET aliases_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(aliases, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                    account_id,
                ),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()
