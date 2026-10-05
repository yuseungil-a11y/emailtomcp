"""MCP 감사 기록 (DESIGN.md §6.1 "감사", §8.6, M6).

- 모든 도구 호출(성공·거부·오류)을 `mcp_audit`에 1건씩 남긴다.
- 인자는 필드 단위로 마스킹한다. 본문류(본문·메모·제목 등)와 긴 문자열은 **길이와
  해시 앞부분만** 남기고 평문은 저장하지 않는다.
- 토큰은 ID와 해시 앞 8자만 남긴다(평문·전체 해시 금지).
- L1(ASGI 가드)에서 도구에 닿기 전에 거부된 요청(401/403/405/413 등)도 남긴다. 다만
  인증 없는 요청이 감사 테이블을 무한히 불리지 못하도록 분당 상한을 두고, 넘치면
  앱 로그에만 남긴다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from emailtomcp.core.models import McpAuditPrincipal, McpAuditResult
from emailtomcp.storage.repositories import mcp as mcp_repo

if TYPE_CHECKING:
    from emailtomcp.core.clock import Clock
    from emailtomcp.mcp_server.auth import Principal
    from emailtomcp.storage.db import Database
    from emailtomcp.storage.repositories.mcp import McpAuditRow

logger = logging.getLogger(__name__)

AUDIT_RETENTION_DAYS = 90

# 내용(=비신뢰 데이터 또는 사용자 작성 본문)이 담기는 필드. 길이+해시로만 남긴다.
_CONTENT_FIELDS = frozenset(
    {"body_text", "body", "note", "subject", "summary", "suggested_reply", "reason"}
)
_MAX_PLAIN_STR = 120
_MAX_LIST_ITEMS = 100
_MAX_DETAIL = 300
HTTP_DENIAL_AUDIT_PER_MINUTE = 30


def _digest(value: str) -> dict[str, Any]:
    return {
        "len": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:16],
    }


def _mask_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        if key in _CONTENT_FIELDS or len(value) > _MAX_PLAIN_STR:
            return _digest(value)
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_LIST_ITEMS:
            return {"count": len(value)}
        return [_mask_value(key, item) for item in value]
    if isinstance(value, dict):
        return {"keys": sorted(str(k) for k in value)[:20]}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"type": type(value).__name__}


def mask_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """도구 인자를 감사용으로 마스킹한다(M6). 원본 dict는 바꾸지 않는다."""
    if not args:
        return {}
    return {str(key): _mask_value(str(key), value) for key, value in args.items()}


class AuditLog:
    """`mcp_audit` 기록기. 블로킹(writer 대기)이므로 executor에서 부른다."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock
        self._denial_lock = threading.Lock()
        self._denial_window_start = 0.0
        self._denial_count = 0

    def record(
        self,
        *,
        principal: Principal | None,
        tool: str | None,
        args: dict[str, Any] | None,
        result: str,
        http_status: int | None,
        detail: str | None = None,
    ) -> None:
        if result not in (McpAuditResult.OK, McpAuditResult.DENIED, McpAuditResult.ERROR):
            raise ValueError(f"알 수 없는 감사 결과값입니다: {result}")
        try:
            mcp_repo.insert_audit(
                self._db,
                ts=self._clock.now().isoformat(),
                principal=principal.kind if principal else McpAuditPrincipal.INTERACTIVE,
                token_id=principal.token_id if principal else None,
                token_hash8=principal.token_hash8 if principal else None,
                job_id=principal.job_id if principal else None,
                tool=tool,
                args_masked_json=json.dumps(mask_args(args), ensure_ascii=False)
                if args is not None
                else None,
                result=result,
                http_status=http_status,
                detail=(detail or "")[:_MAX_DETAIL] or None,
            )
        except Exception:  # noqa: BLE001 — 감사 기록 실패가 요청 처리를 막으면 안 된다(로그로 남김)
            logger.exception("MCP 감사 기록 실패: tool=%s result=%s", tool, result)

    def record_http_denial(self, *, http_status: int, reason: str) -> None:
        """도구 계층에 닿기 전(L1) 거부. 분당 상한을 넘으면 앱 로그에만 남긴다."""
        now = self._clock.monotonic()
        with self._denial_lock:
            if now - self._denial_window_start >= 60.0:
                self._denial_window_start = now
                self._denial_count = 0
            self._denial_count += 1
            over_limit = self._denial_count > HTTP_DENIAL_AUDIT_PER_MINUTE
        if over_limit:
            logger.warning(
                "MCP 요청 거부(감사 상한 초과로 로그만 기록): %s %s", http_status, reason
            )
            return
        self.record(
            principal=None,
            tool=None,
            args=None,
            result=McpAuditResult.DENIED,
            http_status=http_status,
            detail=f"L1: {reason}",
        )

    def list_recent(self, limit: int = 200) -> list[McpAuditRow]:
        conn = self._db.new_read_connection()
        try:
            return mcp_repo.list_audit(conn, limit=limit)
        finally:
            conn.close()

    def purge_expired(self, retention_days: int = AUDIT_RETENTION_DAYS) -> int:
        before = self._clock.now() - timedelta(days=retention_days)
        return mcp_repo.purge_audit_before(self._db, before.isoformat())
