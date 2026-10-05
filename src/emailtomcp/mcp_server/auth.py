"""대화형 MCP 토큰 발급·검증·폐기 (DESIGN.md §6.5, §8.3, H4, M11).

- 토큰은 `secrets.token_urlsafe(32)`(256비트)로 만든다.
- DB(`mcp_tokens`)에는 sha256만 저장한다. 비교는 `hmac.compare_digest`로 한다.
- **proxy 토큰**: 평문은 keyring(`mcp:proxy_token:<프로필>`)에만 둔다. stdio 프록시가
  keyring에서 직접 읽으므로 설정 파일·argv·클립보드에 남지 않는다.
- **http 토큰**(HTTP 직결, 고급): 발급할 때 평문을 한 번만 돌려준다(UI가 1회 표시).
- 기본 만료는 180일, 폐기는 즉시 반영된다(다음 요청부터 401).
- 감사에는 토큰 ID와 해시 앞 8자만 남긴다.

keyring 접근은 `core.ports.SecretStore` 포트로만 한다 — 구현체(`secrets.keyring_store`)는
app.py/`__main__`이 주입한다(§4.2: mcp_server는 secrets 패키지를 직접 모른다).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from emailtomcp.core.models import McpAuditPrincipal, McpTokenKind
from emailtomcp.mcp_server.scopes import INTERACTIVE_SCOPES, validate_interactive_scopes
from emailtomcp.storage.repositories import mcp as mcp_repo

if TYPE_CHECKING:
    from emailtomcp.core.clock import Clock
    from emailtomcp.core.ports import SecretStore
    from emailtomcp.storage.db import Database
    from emailtomcp.storage.repositories.mcp import McpTokenRow

logger = logging.getLogger(__name__)

# `secrets.keyring_store.SERVICE_NAME`과 같은 값이다(의존 규칙상 import하지 않고 값만 맞춘다).
SECRET_SERVICE = "EmailToMCP"
HANDSHAKE_SECRET_KEY = "mcp:handshake_secret"
DEFAULT_PROXY_CLIENT = "default"
DEFAULT_TOKEN_TTL_DAYS = 180

# 마지막 사용 시각은 요청마다 쓰지 않고 이 간격 이상 지났을 때만 갱신한다(writer 부하 억제).
_TOUCH_INTERVAL = timedelta(seconds=60)
# 비정상적으로 긴 Authorization 값은 해시하기 전에 거부한다.
MAX_PRESENTED_TOKEN_LEN = 256
_PROXY_CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def proxy_token_key(client: str) -> str:
    """프록시 프로필별 keyring 키(`--client <name>`, 기본 `default`)."""
    return f"mcp:proxy_token:{client}"


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def validate_proxy_client(client: str) -> str:
    if not _PROXY_CLIENT_RE.match(client):
        raise ValueError("프록시 프로필 이름은 영문·숫자·_·- 1~32자여야 합니다")
    return client


@dataclass(frozen=True, slots=True)
class Principal:
    """인증된 요청 주체. ASGI 가드가 request scope(`state["principal"]`)에 붙인다."""

    kind: str  # McpAuditPrincipal: "interactive" | "job"(P3)
    scopes: frozenset[str]
    token_id: int | None = None
    token_hash8: str | None = None
    label: str | None = None
    job_id: int | None = None


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token_id: int
    kind: str
    # http 토큰일 때만 평문을 담는다(1회 표시). proxy 토큰은 keyring에만 있으므로 None.
    plaintext: str | None


def ensure_handshake_secret(secret_store: SecretStore) -> bytes:
    """핸드셰이크 공유비밀(§6.4, §8.3)을 keyring에서 읽고, 없으면 만든다(앱 쪽에서만 호출)."""
    existing = secret_store.get(SECRET_SERVICE, HANDSHAKE_SECRET_KEY)
    if existing:
        return bytes.fromhex(existing)
    value = secrets.token_bytes(32)
    secret_store.set(SECRET_SERVICE, HANDSHAKE_SECRET_KEY, value.hex())
    return value


def read_handshake_secret(secret_store: SecretStore) -> bytes | None:
    """프록시 쪽: 공유비밀을 읽기만 한다(없으면 None — 앱을 한 번도 실행하지 않은 상태)."""
    existing = secret_store.get(SECRET_SERVICE, HANDSHAKE_SECRET_KEY)
    if not existing:
        return None
    try:
        return bytes.fromhex(existing)
    except ValueError:
        return None


class TokenStore:
    """대화형 토큰 저장소. 모든 메서드는 블로킹(DB/keyring)이므로 executor에서 부른다."""

    def __init__(self, db: Database, secret_store: SecretStore, clock: Clock) -> None:
        self._db = db
        self._secret_store = secret_store
        self._clock = clock

    # ---------------------------------------------------------------- 발급/폐기

    def issue(
        self,
        *,
        kind: str,
        scopes: list[str],
        label: str | None = None,
        client: str = DEFAULT_PROXY_CLIENT,
        ttl_days: int = DEFAULT_TOKEN_TTL_DAYS,
    ) -> IssuedToken:
        """토큰을 발급한다.

        - proxy: `label`은 프록시 프로필 이름(`client`)이 된다. 같은 프로필의 기존 proxy
          토큰은 폐기하고 keyring 값을 새 토큰으로 바꾼다(프로필당 유효 토큰 1개).
        - http: `label`은 사용자가 붙이는 이름이다. 평문을 1회 돌려준다.
        """
        if kind not in (McpTokenKind.PROXY, McpTokenKind.HTTP):
            raise ValueError(f"알 수 없는 토큰 종류입니다: {kind}")
        if ttl_days < 1 or ttl_days > 3650:
            raise ValueError("만료 기간은 1~3650일이어야 합니다")
        checked_scopes = validate_interactive_scopes(scopes)
        if kind == McpTokenKind.PROXY:
            label = validate_proxy_client(client)
        else:
            label = (label or "").strip()
            if not label or len(label) > 100:
                raise ValueError("토큰 라벨은 1~100자여야 합니다")

        now = self._clock.now()
        plaintext = secrets.token_urlsafe(32)
        token_id = mcp_repo.insert_token(
            self._db,
            label=label,
            kind=kind,
            scopes=checked_scopes,
            token_sha256=hash_token(plaintext),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=ttl_days)).isoformat(),
        )

        if kind == McpTokenKind.PROXY:
            try:
                self._secret_store.set(SECRET_SERVICE, proxy_token_key(label), plaintext)
            except Exception:
                # keyring에 못 넣었으면 평문을 어디에도 둘 수 없으므로 방금 만든 토큰을 무효화한다.
                mcp_repo.revoke_token(self._db, token_id, now.isoformat())
                raise
            conn = self._db.new_read_connection()
            try:
                previous = mcp_repo.list_active_proxy_tokens(conn, label)
            finally:
                conn.close()
            for old in previous:
                if old.id != token_id:
                    mcp_repo.revoke_token(self._db, old.id, now.isoformat())
            logger.info("MCP proxy 토큰 발급: id=%s profile=%s", token_id, label)
            return IssuedToken(token_id=token_id, kind=kind, plaintext=None)

        logger.info("MCP http 토큰 발급: id=%s", token_id)
        return IssuedToken(token_id=token_id, kind=kind, plaintext=plaintext)

    def revoke(self, token_id: int) -> bool:
        """토큰을 폐기한다. proxy 토큰이면 keyring에 남은 평문도 지운다."""
        conn = self._db.new_read_connection()
        try:
            row = mcp_repo.get_token(conn, token_id)
        finally:
            conn.close()
        if row is None:
            return False
        changed = mcp_repo.revoke_token(self._db, token_id, self._clock.now().isoformat())
        if row.kind == McpTokenKind.PROXY:
            stored = self._secret_store.get(SECRET_SERVICE, proxy_token_key(row.label))
            if stored and hmac.compare_digest(hash_token(stored), row.token_sha256):
                self._secret_store.delete(SECRET_SERVICE, proxy_token_key(row.label))
        logger.info("MCP 토큰 폐기: id=%s", token_id)
        return changed

    def list_tokens(self) -> list[McpTokenRow]:
        conn = self._db.new_read_connection()
        try:
            return mcp_repo.list_tokens(conn)
        finally:
            conn.close()

    # ---------------------------------------------------------------- 검증

    def authenticate(self, presented: str) -> Principal | None:
        """Bearer 값을 검증해 주체를 돌려준다. 없음·불일치·폐기·만료면 None."""
        if not presented or len(presented) > MAX_PRESENTED_TOKEN_LEN:
            return None
        digest = hash_token(presented)
        conn = self._db.new_read_connection()
        try:
            row = mcp_repo.find_token_by_hash(conn, digest)
        finally:
            conn.close()
        if row is None or not hmac.compare_digest(row.token_sha256, digest):
            return None
        now = self._clock.now()
        if row.revoked_at is not None:
            return None
        if row.expires_at is not None and _parse_iso(row.expires_at) <= now:
            return None
        self._maybe_touch(row, now)
        return Principal(
            kind=McpAuditPrincipal.INTERACTIVE,
            scopes=frozenset(s for s in row.scopes if s in INTERACTIVE_SCOPES),
            token_id=row.id,
            token_hash8=digest[:8],
            label=row.label,
        )

    def _maybe_touch(self, row: McpTokenRow, now: datetime) -> None:
        if row.last_used_at is not None:
            try:
                if now - _parse_iso(row.last_used_at) < _TOUCH_INTERVAL:
                    return
            except ValueError:
                pass
        try:
            mcp_repo.touch_token(self._db, row.id, now.isoformat())
        except Exception:  # noqa: BLE001 — 마지막 사용 시각 갱신 실패가 인증을 막으면 안 된다
            logger.warning("MCP 토큰 마지막 사용 시각 갱신 실패: id=%s", row.id, exc_info=True)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
