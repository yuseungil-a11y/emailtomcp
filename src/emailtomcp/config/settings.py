"""`settings` 테이블 래퍼.

설정은 모두 DB의 `settings(key, value_json)` 테이블 하나로 모은다(소크라테스 §5.3).
이 모듈은 raw sqlite3를 직접 열지 않는다 — 읽기는 호출자가 넘겨준 연결을 쓰고,
쓰기는 항상 `storage.db.Database.writer`를 거쳐 단일 writer 원칙을 지킨다.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    import sqlite3

    from emailtomcp.storage.db import Database

logger = logging.getLogger(__name__)


class SettingEntry(BaseModel):
    """settings 테이블 한 행. value는 JSON 직렬화 가능한 값이어야 한다."""

    model_config = ConfigDict(frozen=True)

    key: str
    value: Any


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """읽기 전용 연결로 설정값 하나를 읽는다. 없으면 default를 반환한다."""
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row[0])


def set_setting(db: Database, key: str, value: Any) -> None:
    """writer 스레드를 거쳐 설정값 하나를 저장(upsert)한다."""
    entry = SettingEntry(key=key, value=value)
    payload = json.dumps(entry.value, ensure_ascii=False)

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO settings(key, value_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (entry.key, payload),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()
