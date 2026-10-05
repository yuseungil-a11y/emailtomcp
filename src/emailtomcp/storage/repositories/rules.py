"""`rules` 테이블 레포지토리 (DESIGN.md §5.1, §7.1, §11.4) — P3 Phase B.

값 검증(조건 스키마, RE2 컴파일, A5 화이트리스트, 고정 템플릿 등)은 호출자(`rules/schema.py`)가
끝내고, 이 모듈은 이미 검증된 값을 저장만 한다(§4.2: storage는 rules를 import하지 않는다).

- **version**: 규칙 내용(조건·동작·응답 방식 등)을 고칠 때마다 +1(M6 감사, §7.0 R-i).
  `update_rule`은 `expected_version` CAS다 — 편집 창을 연 뒤 다른 곳에서 바뀌었으면 거부한다.
  활성화 토글과 우선순위 변경은 내용 변경이 아니므로 version을 올리지 않는다(활성화 여부는
  gate가 `rule_disabled`로 따로 본다).
- **설정 변경 드레인(§7.9 M-A)**: 규칙 저장·삭제·비활성 트랜잭션은 같은 트랜잭션에서
  `drain_autosend_outbox(rule_id=...)`를 호출한다. Phase B에는 policy 자동발송 행이 생기지 않아
  항상 0건이지만, Phase C에서 G7이 열려도 이 2차 방어가 빠지지 않도록 지금 붙여 둔다.
- **삭제**: 진행 중(queued·running)인 그 규칙의 잡을 같은 트랜잭션에서 cancelled
  (reason=`rule_deleted`)로 바꾸고, 남은 잡 이력의 rule_id를 NULL로 끊은 뒤 지운다
  (auto_reply_jobs.rule_id가 rules(id)를 FK로 참조하기 때문). rule_id가 NULL인 잡은 gate가
  규칙을 검사하지 않으므로(미매칭 draft 잡과 같은 모양), **취소를 먼저** 해야 fail-open이 생기지
  않는다. auto_reply_log는 FK가 없어 rule_id·rule_version이 감사 기록으로 그대로 남는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from emailtomcp.core.errors import PermanentError, PolicyError
from emailtomcp.storage.repositories.autoreply import (
    cancel_active_jobs_for_rule,
    drain_autosend_outbox,
)

if TYPE_CHECKING:
    from emailtomcp.storage.db import Database

RULE_VERSION_CONFLICT = "다른 곳에서 규칙이 바뀌었습니다. 다시 열어 확인하세요"

# 내용 필드(바뀌면 version +1). 순서는 INSERT 컬럼 순서와 같다.
_CONTENT_FIELDS: tuple[str, ...] = (
    "name",
    "account_id",
    "conditions_json",
    "action",
    "reply_mode",
    "fixed_template",
    "extra_instructions",
    "max_chars",
    "allow_thread_context",
    "accept_aligned_dkim",
    "cooldown_hours",
)


@dataclass(frozen=True, slots=True)
class RuleRow:
    id: int
    name: str
    account_id: int | None
    enabled: bool
    priority: int
    conditions_json: str
    action: str
    reply_mode: str
    fixed_template: str | None
    extra_instructions: str | None
    max_chars: int
    allow_thread_context: bool
    accept_aligned_dkim: bool
    cooldown_hours: int | None
    version: int
    created_at: str
    updated_at: str


def _row_to_rule(row: sqlite3.Row) -> RuleRow:
    return RuleRow(
        id=row["id"],
        name=row["name"],
        account_id=row["account_id"],
        enabled=bool(row["enabled"]),
        priority=int(row["priority"]),
        conditions_json=row["conditions_json"],
        action=row["action"],
        reply_mode=row["reply_mode"],
        fixed_template=row["fixed_template"],
        extra_instructions=row["extra_instructions"],
        max_chars=int(row["max_chars"]),
        allow_thread_context=bool(row["allow_thread_context"]),
        accept_aligned_dkim=bool(row["accept_aligned_dkim"]),
        cooldown_hours=row["cooldown_hours"],
        version=int(row["version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_rules(conn: sqlite3.Connection) -> list[RuleRow]:
    rows = conn.execute("SELECT * FROM rules ORDER BY priority, id").fetchall()
    return [_row_to_rule(row) for row in rows]


def get_rule(conn: sqlite3.Connection, rule_id: int) -> RuleRow | None:
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return _row_to_rule(row) if row else None


def _content_values(fields: dict[str, Any]) -> list[object]:
    missing = [name for name in _CONTENT_FIELDS if name not in fields]
    if missing:
        raise PermanentError(f"규칙 필드가 빠졌습니다: {', '.join(missing)}")
    values: list[object] = []
    for name in _CONTENT_FIELDS:
        value = fields[name]
        if isinstance(value, bool):
            value = int(value)
        values.append(value)
    return values


def _run(db: Database, fn):  # noqa: ANN001, ANN202 — 내부 전용
    def _work(conn: sqlite3.Connection):  # noqa: ANN202
        conn.execute("BEGIN")
        try:
            result = fn(conn)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return result

    return db.writer.submit(_work).result()


def insert_rule(db: Database, *, fields: dict[str, Any], enabled: bool = True) -> int:
    """새 규칙. priority는 현재 최대값 + 10(목록 맨 뒤)."""
    values = _content_values(fields)
    now = datetime.now(UTC).isoformat()

    def _txn(conn: sqlite3.Connection) -> int:
        top = conn.execute("SELECT COALESCE(MAX(priority), 0) FROM rules").fetchone()[0]
        cursor = conn.execute(
            f"INSERT INTO rules ({', '.join(_CONTENT_FIELDS)}, enabled, priority, version, "
            "created_at, updated_at) "
            f"VALUES ({', '.join('?' for _ in _CONTENT_FIELDS)}, ?, ?, 1, ?, ?)",
            (*values, int(enabled), int(top) + 10, now, now),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    return _run(db, _txn)


def update_rule(
    db: Database, rule_id: int, *, expected_version: int, fields: dict[str, Any]
) -> int:
    """규칙 내용을 고치고 새 version을 돌려준다(CAS). 다르면 PolicyError(아무것도 안 바꿈)."""
    values = _content_values(fields)
    now = datetime.now(UTC).isoformat()
    assignments = ", ".join(f"{name} = ?" for name in _CONTENT_FIELDS)

    def _txn(conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            f"UPDATE rules SET {assignments}, version = version + 1, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (*values, now, rule_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise PolicyError(RULE_VERSION_CONFLICT)
        drain_autosend_outbox(conn, account_id=None, rule_id=rule_id, reason="rule_changed")
        return int(expected_version) + 1

    return _run(db, _txn)


def set_rule_enabled(db: Database, rule_id: int, enabled: bool) -> bool:
    def _txn(conn: sqlite3.Connection) -> bool:
        cursor = conn.execute(
            "UPDATE rules SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(bool(enabled)), datetime.now(UTC).isoformat(), rule_id),
        )
        if not enabled:
            drain_autosend_outbox(conn, account_id=None, rule_id=rule_id, reason="rule_disabled")
        return cursor.rowcount == 1

    return _run(db, _txn)


def set_priorities(db: Database, ordered_ids: list[int]) -> None:
    """목록 순서대로 priority를 10, 20, 30…으로 다시 매긴다(드래그 정렬, §11.4)."""

    def _txn(conn: sqlite3.Connection) -> None:
        now = datetime.now(UTC).isoformat()
        for index, rule_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE rules SET priority = ?, updated_at = ? WHERE id = ?",
                ((index + 1) * 10, now, int(rule_id)),
            )

    _run(db, _txn)


def delete_rule(db: Database, rule_id: int) -> bool:
    """규칙을 지운다. 진행 중인 그 규칙의 잡을 먼저 취소한다(모듈 docstring 참고)."""
    now_iso = datetime.now(UTC).isoformat()

    def _txn(conn: sqlite3.Connection) -> bool:
        if conn.execute("SELECT 1 FROM rules WHERE id = ?", (rule_id,)).fetchone() is None:
            return False
        cancel_active_jobs_for_rule(conn, rule_id=rule_id, reason="rule_deleted", now_iso=now_iso)
        drain_autosend_outbox(conn, account_id=None, rule_id=rule_id, reason="rule_deleted")
        conn.execute("UPDATE auto_reply_jobs SET rule_id = NULL WHERE rule_id = ?", (rule_id,))
        cursor = conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        return cursor.rowcount == 1

    return _run(db, _txn)
