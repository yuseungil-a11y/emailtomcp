"""L1 요청 검증 ASGI 미들웨어 (DESIGN.md §6.1 D-08, §8.1 TB2, L1·M2·H4).

SDK 앱보다 **먼저** 실행되어, 도구 계층에 닿기 전에 아래를 순서대로 검사한다.

| 순서 | 검사 | 실패 응답 |
|---|---|---|
| ① | 메서드는 POST만(OPTIONS·GET·DELETE 등 거부, CORS 헤더는 절대 응답하지 않음) | 405 |
| ② | 경로는 `/mcp`만 | 404 |
| ③ | `Content-Type: application/json`만 / 본문 1MB 상한 | 415 / 413 |
| ④ | Host가 `127.0.0.1:<port>` 또는 `localhost:<port>`와 **포트까지** 정확히 일치 | 403 |
| ⑤ | Origin 헤더가 있으면 값과 무관하게 거부(`null`, 빈 값 포함) | 403 |
| ⑥ | `Authorization: Bearer <토큰>` 검증(`auth.TokenStore`가 `compare_digest`로 비교) | 401 |

통과하면 주체(`auth.Principal`)를 `scope["state"]["principal"]`에 붙여 SDK 앱에 넘긴다.
SDK의 저수준 handler는 `ctx.request.scope["state"]`에서 이 값을 읽는다(L3).

L2(SDK `transport_security`)의 허용 Host 목록도 이 모듈의 `allowed_hosts(port)` 하나에서
만든다 — 두 계층 설정이 어긋나 한쪽이 느슨해지는 것을 막기 위해서다(§6.1).

이 가드로 SDK 앱을 감싸면 SDK 내장 lifespan이 실행되지 않는다. 세션 매니저
(`server.session_manager.run()`)는 호스트(`runtime/asgi_host.py`)가 직접 열고 닫는다(D-06).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from emailtomcp.core.models import McpAuditPrincipal

if TYPE_CHECKING:
    from emailtomcp.core.clock import Clock
    from emailtomcp.core.events import EventBus
    from emailtomcp.mcp_server.auth import Principal

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
MAX_BODY_BYTES = 1 * 1024 * 1024
LOOPBACK_HOST = "127.0.0.1"

EVENT_MCP_CLIENT_CONNECTED = "McpClientConnected"
EVENT_MCP_CLIENT_DISCONNECTED = "McpClientDisconnected"
DEFAULT_CLIENT_IDLE_SECONDS = 300.0

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

Authenticator = Callable[[str], Awaitable["Principal | None"]]
DenialHook = Callable[[int, str], Awaitable[None]]
AuthenticatedHook = Callable[["Principal"], None]


def allowed_hosts(port: int) -> list[str]:
    """L1·L2 공용 허용 Host 목록(포트까지 정확히). `[::1]`은 바인딩하지 않으므로 넣지 않는다."""
    return [f"{LOOPBACK_HOST}:{port}", f"localhost:{port}"]


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope.get("headers", []) if key.lower() == name]


async def _send_plain(
    send: Send, status: int, text: str, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    body = text.encode("utf-8")
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class AsgiGuard:
    """SDK의 streamable HTTP 앱을 감싸는 L1 검증기."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        port: int,
        authenticate: Authenticator,
        on_denied: DenialHook | None = None,
        on_authenticated: AuthenticatedHook | None = None,
        max_body_bytes: int = MAX_BODY_BYTES,
        path: str = MCP_PATH,
    ) -> None:
        self._app = app
        self._allowed_hosts = frozenset(h.lower() for h in allowed_hosts(port))
        self._authenticate = authenticate
        self._on_denied = on_denied
        self._on_authenticated = on_authenticated
        self._max_body = max_body_bytes
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        if scope_type != "http":
            # websocket 등은 지원하지 않는다.
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        await self._handle_http(scope, receive, send)

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        """SDK 내장 lifespan을 대신 실행하지 않는다(세션 매니저는 호스트가 연다, D-06)."""
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _deny(
        self,
        send: Send,
        status: int,
        reason: str,
        text: str,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        if self._on_denied is not None:
            try:
                await self._on_denied(status, reason)
            except Exception:  # noqa: BLE001 — 감사 실패가 거부 응답을 막으면 안 된다
                logger.warning("MCP 거부 감사 기록 실패", exc_info=True)
        await _send_plain(send, status, text, extra_headers)

    async def _handle_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        # ① 메서드
        if scope.get("method") != "POST":
            await self._deny(send, 405, "method", "Method Not Allowed", [(b"allow", b"POST")])
            return
        # ② 경로
        if scope.get("path") != self._path:
            await self._deny(send, 404, "path", "Not Found")
            return
        # ③ Content-Type / 크기(선언값)
        content_types = _header_values(scope, b"content-type")
        if len(content_types) != 1 or (
            content_types[0].decode("latin-1").split(";")[0].strip().lower() != "application/json"
        ):
            await self._deny(send, 415, "content_type", "Unsupported Media Type")
            return
        lengths = _header_values(scope, b"content-length")
        if lengths:
            try:
                declared = int(lengths[0])
            except ValueError:
                await self._deny(send, 400, "content_length", "Bad Request")
                return
            if declared > self._max_body:
                await self._deny(send, 413, "body_too_large", "Payload Too Large")
                return
        # ④ Host(포트까지 정확히, 헤더는 정확히 1개)
        hosts = _header_values(scope, b"host")
        if len(hosts) != 1 or hosts[0].decode("latin-1").strip().lower() not in self._allowed_hosts:
            await self._deny(send, 403, "host", "Forbidden")
            return
        # ⑤ Origin(존재 자체를 거부, `null`·빈 값 포함)
        if _header_values(scope, b"origin"):
            await self._deny(send, 403, "origin", "Forbidden")
            return
        # ⑥ Bearer
        auth_values = _header_values(scope, b"authorization")
        token = ""
        if len(auth_values) == 1:
            raw = auth_values[0].decode("latin-1")
            scheme, _, value = raw.partition(" ")
            if scheme.lower() == "bearer":
                token = value.strip()
        principal = await self._authenticate(token) if token else None
        if principal is None:
            await self._deny(
                send, 401, "unauthorized", "Unauthorized", [(b"www-authenticate", b"Bearer")]
            )
            return

        # 본문을 상한까지만 읽는다(청크 인코딩 등 선언값이 없거나 거짓인 경우 대비).
        chunks: list[bytes] = []
        total = 0
        trailing: list[Message] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                trailing.append(message)
                break
            body = message.get("body", b"")
            total += len(body)
            if total > self._max_body:
                await self._deny(send, 413, "body_too_large", "Payload Too Large")
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        replay: list[Message] = [
            {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        ]
        replay.extend(trailing)

        async def _replay_receive() -> Message:
            if replay:
                return replay.pop(0)
            return await receive()

        state = scope.setdefault("state", {})
        state["principal"] = principal
        if self._on_authenticated is not None:
            try:
                self._on_authenticated(principal)
            except Exception:  # noqa: BLE001
                logger.warning("MCP 활동 기록 실패", exc_info=True)
        await self._app(scope, _replay_receive, send)


class ActivityTracker:
    """대화형 클라이언트 연결 상태(§4.2 McpClientConnected/Disconnected).

    가드가 인증에 성공할 때마다 `touch()`로 마지막 활동 시각을 갱신한다. 끊겨 있던 상태에서
    요청이 오면 Connected를, 마지막 요청 이후 `idle_seconds`(기본 5분) 동안 요청이 없으면
    `check_idle()`이 Disconnected를 발행한다. `check_idle()`은 백엔드 주기 작업이 부른다.
    서버가 켜져 있는지(`McpStatusChanged`)와는 별개 신호다.
    """

    def __init__(
        self,
        event_bus: EventBus,
        clock: Clock,
        *,
        idle_seconds: float = DEFAULT_CLIENT_IDLE_SECONDS,
    ) -> None:
        self._event_bus = event_bus
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._connected = False
        self._last_activity = 0.0
        self._last_label: str | None = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def last_activity_at(self) -> float | None:
        with self._lock:
            return self._last_activity if self._connected else None

    def touch(self, principal: Principal) -> None:
        if principal.kind != McpAuditPrincipal.INTERACTIVE:
            return
        with self._lock:
            was_connected = self._connected
            self._connected = True
            self._last_activity = self._clock.monotonic()
            self._last_label = principal.label
        if not was_connected:
            self._event_bus.publish(
                EVENT_MCP_CLIENT_CONNECTED,
                {
                    "token_id": principal.token_id,
                    "label": principal.label,
                    "at": self._clock.now().isoformat(),
                },
            )

    def check_idle(self) -> bool:
        """유휴 시간이 지났으면 Disconnected를 발행하고 True를 돌려준다."""
        with self._lock:
            if not self._connected:
                return False
            if self._clock.monotonic() - self._last_activity < self._idle_seconds:
                return False
            self._connected = False
            label = self._last_label
        self._event_bus.publish(
            EVENT_MCP_CLIENT_DISCONNECTED, {"label": label, "at": self._clock.now().isoformat()}
        )
        return True


def run_blocking_hook(fn: Callable[..., Any], *args: Any) -> Awaitable[Any]:
    """블로킹 콜백(DB 감사 등)을 백엔드 executor에서 실행하는 헬퍼(기본 to_thread 금지, S-01)."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, lambda: fn(*args))
