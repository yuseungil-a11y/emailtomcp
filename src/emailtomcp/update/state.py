"""업데이트 로컬 상태 저장 (DESIGN.md §14.4 10단계, §11.7, §11.8).

`settings` 테이블(키-값 JSON)에 다음 키로 저장한다. 읽기/쓰기는 `config.settings`의
`get_setting`/`set_setting`을 그대로 쓴다(단일 writer 원칙).

| 키 | 내용 |
|---|---|
| `update.last_issued_at` | 마지막으로 수락한 매니페스트의 issued_at(ISO 8601). 롤백 방어([6]) |
| `update.max_seen_version` | 검증된 매니페스트로 본 최고 버전(local floor, §14.5) |
| `update.seen_hashes` | {버전: {slot: {name, sha256}}} 최근 20개 버전. 해시 충돌 탐지([9]) |
| `update.state` | 마지막 확인 결과(UI 표시용 스냅샷, `UpdateStatus.to_dict()`) |
| `update.auto_check` | 자동 확인 on/off. 저장값이 없으면 기본값(정식 빌드 on, dev 빌드 off) |

`last_issued_at`/`max_seen_version`/`seen_hashes`는 **검증을 통과한 매니페스트로만** 갱신한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from emailtomcp.config.settings import get_setting, set_setting
from emailtomcp.update.version import parse_version

if TYPE_CHECKING:
    from emailtomcp.storage.db import Database

logger = logging.getLogger(__name__)

KEY_LAST_ISSUED_AT = "update.last_issued_at"
KEY_MAX_SEEN_VERSION = "update.max_seen_version"
KEY_SEEN_HASHES = "update.seen_hashes"
KEY_STATE = "update.state"
KEY_AUTO_CHECK = "update.auto_check"

SEEN_HASHES_LIMIT = 20

SeenHashes = dict[str, dict[str, dict[str, str]]]


@dataclass(slots=True)
class TrustState:
    """롤백/해시충돌 방어에 쓰는 신뢰 상태(검증된 매니페스트로만 갱신)."""

    last_issued_at: datetime | None = None
    max_seen_version: str | None = None
    seen_hashes: SeenHashes = field(default_factory=dict)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _sanitize_seen_hashes(value: Any) -> SeenHashes:
    """DB에서 읽은 값을 방어적으로 정리한다(형식이 깨졌으면 그 항목만 버린다)."""
    result: SeenHashes = {}
    if not isinstance(value, dict):
        return result
    for version, slots in value.items():
        if not isinstance(version, str) or not isinstance(slots, dict):
            continue
        clean: dict[str, dict[str, str]] = {}
        for slot, entry in slots.items():
            if (
                isinstance(slot, str)
                and isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and isinstance(entry.get("sha256"), str)
            ):
                clean[slot] = {"name": entry["name"], "sha256": entry["sha256"]}
        result[version] = clean
    return result


def merge_seen_hashes(
    seen: SeenHashes, version: str, slots: dict[str, dict[str, str]]
) -> SeenHashes:
    """버전의 산출물 해시를 병합하고 최근 `SEEN_HASHES_LIMIT`개 버전만 남긴다.

    (해시 충돌 검사는 verifier가 먼저 끝낸 뒤 호출한다 — 여기서는 합치기만 한다.)
    """
    merged = {k: dict(v) for k, v in seen.items() if k != version}
    combined = dict(seen.get(version, {}))
    combined.update(slots)
    merged[version] = combined  # 최근 본 버전을 맨 뒤로
    while len(merged) > SEEN_HASHES_LIMIT:
        merged.pop(next(iter(merged)))
    return merged


class UpdateStateStore:
    """settings 테이블 위의 얇은 저장소."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _get(self, key: str, default: Any = None) -> Any:
        conn = self._db.new_read_connection()
        try:
            return get_setting(conn, key, default)
        finally:
            conn.close()

    # ------------------------------------------------------------ 신뢰 상태
    def load_trust(self) -> TrustState:
        max_seen = self._get(KEY_MAX_SEEN_VERSION)
        if max_seen is not None and parse_version(max_seen) is None:
            logger.warning("저장된 update.max_seen_version이 올바르지 않아 무시합니다")
            max_seen = None
        return TrustState(
            last_issued_at=_parse_dt(self._get(KEY_LAST_ISSUED_AT)),
            max_seen_version=max_seen,
            seen_hashes=_sanitize_seen_hashes(self._get(KEY_SEEN_HASHES, {})),
        )

    def save_trust(self, trust: TrustState) -> None:
        # 해시 → 최고버전 → issued_at 순서로 쓴다. 중간에 끊겨도 다음 확인에서 issued_at이
        # 갱신되지 않은 채 같은 매니페스트를 다시 수락할 뿐이라 안전 쪽으로 실패한다.
        set_setting(self._db, KEY_SEEN_HASHES, trust.seen_hashes)
        set_setting(self._db, KEY_MAX_SEEN_VERSION, trust.max_seen_version)
        set_setting(
            self._db,
            KEY_LAST_ISSUED_AT,
            trust.last_issued_at.isoformat() if trust.last_issued_at else None,
        )

    # ------------------------------------------------------------ UI 스냅샷
    def load_status(self) -> dict[str, Any] | None:
        value = self._get(KEY_STATE)
        return value if isinstance(value, dict) else None

    def save_status(self, status: dict[str, Any]) -> None:
        set_setting(self._db, KEY_STATE, status)

    # ------------------------------------------------------------ 자동 확인
    def get_auto_check(self, default: bool) -> bool:
        value = self._get(KEY_AUTO_CHECK)
        return value if isinstance(value, bool) else default

    def set_auto_check(self, enabled: bool) -> None:
        set_setting(self._db, KEY_AUTO_CHECK, bool(enabled))
