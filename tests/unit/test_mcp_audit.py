"""MCP 감사 기록 단위 테스트 (DESIGN.md §6.1 "감사", §8.6, M6, 갈릴레오 C2 커버리지 보강).

`tests/unit/test_mcp_core.py`의 `mask_args` 기본 테스트에 이어, dict/미지원 타입 마스킹,
`AuditLog.record`의 유효성 검증·예외 흡수, `record_http_denial`의 분당 상한, 조회/정리를
다룬다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emailtomcp.core.clock import FakeClock
from emailtomcp.core.models import McpAuditPrincipal, McpAuditResult
from emailtomcp.mcp_server.audit import HTTP_DENIAL_AUDIT_PER_MINUTE, AuditLog, mask_args
from emailtomcp.mcp_server.auth import Principal
from mcp_support import make_db

pytestmark = pytest.mark.unit


def test_mask_args_handles_dict_and_unknown_types() -> None:
    masked = mask_args(
        {
            "options": {"secret": "x", "other": 1},
            "weird": {1, 2, 3},  # set: 지원하지 않는 타입
        }
    )
    assert masked["options"] == {"keys": ["other", "secret"]}
    assert masked["weird"] == {"type": "set"}


def test_mask_args_empty_or_none_returns_empty_dict() -> None:
    assert mask_args(None) == {}
    assert mask_args({}) == {}


def _audit(tmp_path: Path) -> tuple[AuditLog, FakeClock, object]:
    db = make_db(tmp_path)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    return AuditLog(db, clock), clock, db


def test_record_rejects_unknown_result_value(tmp_path: Path) -> None:
    audit, _clock, db = _audit(tmp_path)
    try:
        with pytest.raises(ValueError, match="알 수 없는 감사 결과값"):
            audit.record(
                principal=None, tool="x", args=None, result="not-a-result", http_status=None
            )
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_record_swallows_insert_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 기록 실패가 요청 처리를 막지 않는다(예외를 삼키고 로그만 남김)."""
    audit, _clock, db = _audit(tmp_path)
    try:

        def _boom(*_a: object, **_kw: object) -> None:
            raise RuntimeError("db unavailable")

        monkeypatch.setattr("emailtomcp.mcp_server.audit.mcp_repo.insert_audit", _boom)
        # 예외가 밖으로 전파되지 않아야 한다.
        audit.record(
            principal=None, tool="list_accounts", args={}, result=McpAuditResult.OK, http_status=200
        )
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_record_with_principal_masks_and_truncates_detail(tmp_path: Path) -> None:
    audit, _clock, db = _audit(tmp_path)
    try:
        principal = Principal(
            kind=McpAuditPrincipal.INTERACTIVE,
            scopes=frozenset({"read"}),
            token_id=7,
            token_hash8="abcd1234",
            label="t",
        )
        audit.record(
            principal=principal,
            tool="get_message",
            args={"message_id": 1},
            result=McpAuditResult.ERROR,
            http_status=400,
            detail="x" * 500,
        )
        rows = audit.list_recent()
        assert len(rows) == 1
        assert rows[0].token_id == 7
        assert rows[0].detail is not None and len(rows[0].detail) <= 300
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_record_http_denial_rate_limited_per_minute(tmp_path: Path) -> None:
    audit, clock, db = _audit(tmp_path)
    try:
        for _ in range(HTTP_DENIAL_AUDIT_PER_MINUTE):
            audit.record_http_denial(http_status=401, reason="unauthorized")
        assert len(audit.list_recent()) == HTTP_DENIAL_AUDIT_PER_MINUTE
        # 상한을 넘기면 더는 기록되지 않는다(로그에만 남음).
        audit.record_http_denial(http_status=401, reason="unauthorized")
        assert len(audit.list_recent()) == HTTP_DENIAL_AUDIT_PER_MINUTE
        # 1분이 지나면 다시 기록된다(창 초기화).
        clock.advance(61)
        audit.record_http_denial(http_status=403, reason="origin")
        assert len(audit.list_recent()) == HTTP_DENIAL_AUDIT_PER_MINUTE + 1
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_purge_expired_removes_old_rows_only(tmp_path: Path) -> None:
    audit, clock, db = _audit(tmp_path)
    try:
        audit.record(principal=None, tool=None, args=None, result=McpAuditResult.DENIED, http_status=401)
        clock.advance(timedelta(days=100).total_seconds())
        audit.record(principal=None, tool=None, args=None, result=McpAuditResult.DENIED, http_status=401)
        purged = audit.purge_expired(retention_days=90)
        assert purged == 1
        assert len(audit.list_recent()) == 1
    finally:
        db.stop()  # type: ignore[attr-defined]
