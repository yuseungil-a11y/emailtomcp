"""자동회신 전역 토글 기반 스키마 (user_version=3, DESIGN.md §5.1 "P3 마이그레이션", §7.0, §7.11).

ADD COLUMN·인덱스·설정 초기값만 다루므로 `rebuild_table` 없이 처리한다(§5.4).

- `auto_reply_jobs.enable_gen INTEGER NOT NULL DEFAULT 0` — 잡을 만들 때의 토글 세대.
  **0은 "세대 없음" 예약값**이다(보안리뷰 L-B). 이 마이그레이션 이전에 만들어진 잡은 모두 0이라
  이후 어떤 세대와도 일치하지 않는다(fail-closed).
- `auto_reply_jobs.submitted_at`, `submission_sha256` — G5 제출 기록(§7.9).
- `ux_jobs_autoreply_msg` — 메일 1건당 auto_reply 잡 1개(재평가·이중 구독에도 멱등).
- `accounts.trusted_boundary_by` — 수신 경계 MTA의 Received `by` 패턴(§7.6 A1·A2).
- `auto_reply_log.recipient_norm`, `thread_key` — LoopGuard 12~14 집계 근거(§7.3 H-A).
- 설정 `autoreply.generation` 1회 초기화(§7.0 세대 규칙). 하한 `floor = max(1,
  COALESCE(MAX(auto_reply_jobs.enable_gen), 0) + 1)`을 구해(보안검토 L-1)
  - 이미 유효한 값(bool이 아닌 1 이상의 정수)이 있으면 그대로 둔다. 단 floor보다 작으면 floor로
    **올린다**(줄이지는 않는다).
  - 값이 없거나 무효(0/음수/비정수/bool/JSON 손상)면 floor로 둔다.
  새 DB와 일반적인 v2 업그레이드에서는 enable_gen 컬럼을 방금 추가해 전부 0이므로 floor=1이다.
  floor가 의미를 갖는 것은 enable_gen 컬럼이 이미 있던(개발 중 수동 추가) DB뿐이며, 그 경우에도
  새 세대가 기존 잡의 enable_gen과 일치할 수 없게 한다(ABA 방지).
- 설정 `autoreply.enabled`는 지운다(I7: 마이그레이션 직후는 off, 키가 없으면 off). 이 마이그레이션
  이전에는 보호 키 장치가 없어 범용 `set_setting`으로 누구나 쓸 수 있었으므로, 그 값을 사람의
  켜기 확인(G0)을 거친 값으로 믿지 않는다.
- 같은 이유로 v2 시절 범용 경로로 쓰였을 수 있는 `autoreply.queue_state`·
  `autoreply.autosend_state`도 지운다(보안검토 L-5). Phase B에서 컨트롤러가 처음부터 다시 관리한다.

테이블이 없을 가능성(m0001이 모두 만들지만 방어적으로)에 대비해 존재 여부를 확인하고, 없는
테이블의 변경은 건너뛴다. 컬럼이 이미 있으면(개발 중 수동 추가 등) 다시 추가하지 않는다.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

# 이 파일과 storage/repositories/autoreply.py 두 곳만 이 키 문자열을 쓴다(아키텍처 테스트).
_GENERATION_KEY = "autoreply.generation"
_ENABLED_KEY = "autoreply.enabled"
# v2 시절 범용 경로로 쓰였을 수 있어 믿지 않고 지우는 키(L-5).
_STALE_KEYS = (_ENABLED_KEY, "autoreply.queue_state", "autoreply.autosend_state")
_INITIAL_GENERATION = 1


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # PRAGMA는 바인딩을 지원하지 않는다. table은 이 모듈의 상수에서만 온다.
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _valid_generation(raw: str) -> int | None:
    """유효한 세대(bool이 아닌 1 이상의 정수)면 그 값, 아니면 None."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    # bool은 int의 하위 타입이므로 명시적으로 제외한다(true를 1로 읽지 않는다).
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _generation_floor(conn: sqlite3.Connection) -> int:
    """`max(1, COALESCE(MAX(auto_reply_jobs.enable_gen), 0) + 1)`(L-1)."""
    max_gen = 0
    if _table_exists(conn, "auto_reply_jobs") and "enable_gen" in _columns(conn, "auto_reply_jobs"):
        row = conn.execute("SELECT COALESCE(MAX(enable_gen), 0) FROM auto_reply_jobs").fetchone()
        max_gen = int(row[0])
    return max(_INITIAL_GENERATION, max_gen + 1)


def upgrade(conn: sqlite3.Connection) -> None:
    """runner.py가 이미 걸어 둔 트랜잭션 안에서 실행된다 — 여기서 commit/rollback하지 않는다."""
    if _table_exists(conn, "auto_reply_jobs"):
        _add_column_if_missing(conn, "auto_reply_jobs", "enable_gen", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "auto_reply_jobs", "submitted_at", "TEXT")
        _add_column_if_missing(conn, "auto_reply_jobs", "submission_sha256", "TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_autoreply_msg "
            "ON auto_reply_jobs(message_id) WHERE kind = 'auto_reply'"
        )
    if _table_exists(conn, "accounts"):
        _add_column_if_missing(conn, "accounts", "trusted_boundary_by", "TEXT")
    if _table_exists(conn, "auto_reply_log"):
        _add_column_if_missing(conn, "auto_reply_log", "recipient_norm", "TEXT")
        _add_column_if_missing(conn, "auto_reply_log", "thread_key", "TEXT")

    if _table_exists(conn, "settings"):
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (_GENERATION_KEY,)
        ).fetchone()
        current = None if row is None else _valid_generation(row[0])
        floor = _generation_floor(conn)
        # 유효값은 보존하되 floor보다 작으면 올린다. 없거나 무효면 floor. 줄이는 경우는 없다
        # (무효값은 "세대"가 아니므로 비교 대상이 아니다).
        target = floor if current is None else max(current, floor)
        if current is None or target != current:
            conn.execute(
                "INSERT INTO settings(key, value_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (_GENERATION_KEY, json.dumps(target)),
            )
        for key in _STALE_KEYS:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
