"""`--mcp-stdio-proxy` 모드 — Claude Code/Desktop ↔ 앱 MCP 서버 중계 (DESIGN.md §6.4, H4·M2·C-05).

- QApplication을 만들지 않고 DB도 열지 않는다. 의존하는 것은 keyring(주입된
  `SecretStore`), 로컬 핸드셰이크, HTTP 클라이언트뿐이다.
- 요청이 올 때마다 앱에 핸드셰이크해(공유비밀 HMAC 검증) **현재 포트**를 알아낸다.
  그래서 앱의 포트를 바꾸거나 앱을 나중에 켜도 Claude 쪽 등록을 다시 할 필요가 없다.
- 토큰은 keyring의 프록시 프로필(`--client <name>`, 기본 `default`)에서 직접 읽는다 —
  `~/.claude.json`, argv, 셸 히스토리, 클립보드에 남지 않는다(H4).
- 도구 호출(list_tools/call_tool)만 중계한다(resources/prompts 없음).
- stdout은 MCP stdio 프로토콜 전용이다. 로그는 stderr로만 보낸다.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

import anyio
import httpx2
import mcp_types as types
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from emailtomcp.core.errors import SecurityError
from emailtomcp.mcp_server.approval import APPROVAL_WAIT_SECONDS
from emailtomcp.mcp_server.asgi_guard import LOOPBACK_HOST, MCP_PATH
from emailtomcp.mcp_server.auth import (
    DEFAULT_PROXY_CLIENT,
    SECRET_SERVICE,
    proxy_token_key,
    read_handshake_secret,
    validate_proxy_client,
)
from emailtomcp.mcp_server.local_handshake import (
    SINGLE_INSTANCE_KEY,
    AppNotRunningError,
    McpDisabledError,
    client_handshake,
)

if TYPE_CHECKING:
    from emailtomcp.core.ports import SecretStore

logger = logging.getLogger(__name__)

T = TypeVar("T")

# send_draft는 승인을 최대 120초 기다리므로 읽기 타임아웃은 그보다 넉넉해야 한다.
HTTP_READ_TIMEOUT = APPROVAL_WAIT_SECONDS + 60.0
HTTP_CONNECT_TIMEOUT = 10.0
PROXY_SERVER_NAME = "emailtomcp-proxy"

# JSON-RPC 오류 코드(서버 측 일반 오류 영역).
_ERR_APP_UNAVAILABLE = -32001
_ERR_SECURITY = -32002
_ERR_CONFIG = -32003
_ERR_UPSTREAM = -32004


class ProxyConfigError(RuntimeError):
    """프록시를 쓸 준비가 안 됨(공유비밀·토큰 없음)."""


HandshakeFn = Callable[[bytes], int]
HttpClientFactory = Callable[[str, dict[str, str]], httpx2.AsyncClient]


def _default_http_client(_base_url: str, headers: dict[str, str]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(HTTP_CONNECT_TIMEOUT, read=HTTP_READ_TIMEOUT),
        follow_redirects=False,
        trust_env=False,  # 시스템 프록시 설정이 loopback 요청을 가로채지 않게 한다.
    )


class ProxyForwarder:
    """요청마다 핸드셰이크 → 앱 MCP 서버에 HTTP로 중계."""

    def __init__(
        self,
        secret_store: SecretStore,
        *,
        client: str = DEFAULT_PROXY_CLIENT,
        handshake: HandshakeFn | None = None,
        http_client_factory: HttpClientFactory | None = None,
        server_name: str = SINGLE_INSTANCE_KEY,
    ) -> None:
        self._secret_store = secret_store
        self._client = validate_proxy_client(client)
        self._handshake = handshake or (
            lambda secret: client_handshake(secret, server_name=server_name)
        )
        self._http_client_factory = http_client_factory or _default_http_client

    def _credentials(self) -> tuple[bytes, str]:
        secret = read_handshake_secret(self._secret_store)
        if secret is None:
            raise ProxyConfigError(
                "EmailToMCP 앱을 한 번 실행해 초기 설정을 마친 뒤 다시 시도하세요(공유비밀 없음)."
            )
        token = self._secret_store.get(SECRET_SERVICE, proxy_token_key(self._client))
        if not token:
            raise ProxyConfigError(
                f"프록시 토큰(프로필 '{self._client}')이 없습니다. 앱의 Claude > MCP 패널 > "
                "토큰 탭에서 '프록시' 토큰을 발급하세요."
            )
        return secret, token

    async def _with_upstream(self, fn: Callable[[Client], Awaitable[T]]) -> T:
        try:
            secret, token = self._credentials()
            port = await anyio.to_thread.run_sync(self._handshake, secret)
        except ProxyConfigError as exc:
            raise MCPError(_ERR_CONFIG, str(exc)) from exc
        except (AppNotRunningError, McpDisabledError) as exc:
            raise MCPError(_ERR_APP_UNAVAILABLE, str(exc)) from exc
        except SecurityError as exc:
            logger.error("핸드셰이크 검증 실패 — 중계를 중단합니다: %s", exc)
            raise MCPError(_ERR_SECURITY, str(exc)) from exc

        base_url = f"http://{LOOPBACK_HOST}:{port}"
        http_client = self._http_client_factory(base_url, {"Authorization": f"Bearer {token}"})
        try:
            async with (
                http_client,
                Client(
                    streamable_http_client(base_url + MCP_PATH, http_client=http_client)
                ) as upstream,
            ):
                return await fn(upstream)
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 — 상세(토큰 포함 가능)는 숨기고 원인만 알린다
            logger.warning("앱 MCP 서버 중계 실패: %s", type(exc).__name__)
            raise MCPError(
                _ERR_UPSTREAM,
                "EmailToMCP 앱의 MCP 서버와 통신하지 못했습니다(토큰 폐기·만료 또는 앱 종료). "
                "앱의 Claude > MCP 패널에서 상태를 확인하세요.",
            ) from exc

    async def list_tools(self) -> types.ListToolsResult:
        async def _do(upstream: Client) -> types.ListToolsResult:
            result = await upstream.list_tools()
            return types.ListToolsResult(tools=list(result.tools))

        return await self._with_upstream(_do)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        async def _do(upstream: Client) -> types.CallToolResult:
            result = await upstream.call_tool(name, arguments or {})
            if not isinstance(result, types.CallToolResult):
                raise MCPError(_ERR_UPSTREAM, "예상하지 못한 응답 형식입니다")
            return result

        return await self._with_upstream(_do)


def build_proxy_server(forwarder: ProxyForwarder) -> Server[Any]:
    async def _on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return await forwarder.list_tools()

    async def _on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            return await forwarder.call_tool(params.name, dict(params.arguments or {}))
        except MCPError as exc:
            # 도구 호출 실패는 모델이 읽을 수 있게 도구 오류 결과로 돌려준다.
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=exc.message)], is_error=True
            )

    return Server(PROXY_SERVER_NAME, on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


def _setup_stderr_logging() -> None:
    root = logging.getLogger("emailtomcp")
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)


def run_stdio_proxy(*, secret_store: SecretStore, client: str = DEFAULT_PROXY_CLIENT) -> int:
    """`__main__`에서 호출한다. stdin이 닫힐 때까지 중계하고 종료 코드를 돌려준다."""
    _setup_stderr_logging()
    try:
        forwarder = ProxyForwarder(secret_store, client=client)
    except ValueError as exc:
        print(f"emailtomcp: {exc}", file=sys.stderr)
        return 2
    server = build_proxy_server(forwarder)

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(_main)
    return 0
