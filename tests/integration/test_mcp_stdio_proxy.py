"""stdio 프록시 중계 (DESIGN.md §6.4, §12.3 P2 "프록시" 항목).

- 프록시 MCP 서버(`build_proxy_server`)에 공식 SDK in-process 클라이언트로 붙고, 프록시는
  ASGITransport로 앱의 MCP 앱에 중계한다(실제 stdin/stdout·소켓 불필요).
- 핸드셰이크는 주입한 함수로 대체해, 포트가 바뀌어도(재등록 없이) 따라가는지, 가짜 서버·
  앱 미실행·토큰 없음 같은 실패가 명확한 오류로 나오는지 확인한다. 실제 QLocalServer
  핸드셰이크는 `tests/ui/test_mcp_local_handshake_qt.py`에서 확인한다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.client.client import Client

from emailtomcp.core.errors import SecurityError
from emailtomcp.mcp_server.auth import ensure_handshake_secret
from emailtomcp.mcp_server.local_handshake import AppNotRunningError, McpDisabledError
from emailtomcp.mcp_server.stdio_proxy import ProxyForwarder, build_proxy_server
from mcp_support import McpEnv, build_env

pytestmark = pytest.mark.integration


def _factory(app: Any, seen_urls: list[str]):  # noqa: ANN202
    def _make(base_url: str, headers: dict[str, str]) -> httpx2.AsyncClient:
        seen_urls.append(base_url)
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=base_url, headers=headers
        )

    return _make


def _setup(env: McpEnv) -> None:
    ensure_handshake_secret(env.secret_store)
    env.components.tokens.issue(kind="proxy", scopes=["read", "draft"], client="default")


def test_proxy_relays_tools_and_follows_port_change(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _setup(env)
    ports = iter([8765, 8765, 9100, 9100])
    seen_urls: list[str] = []

    async def _run() -> tuple[list[str], str, str]:
        app_a = env.components.build_asgi(8765)
        async with env.components.tool_server.session_manager.run():
            forwarder = ProxyForwarder(
                env.secret_store,
                handshake=lambda _secret: next(ports),
                http_client_factory=_factory(app_a, seen_urls),
            )
            async with Client(build_proxy_server(forwarder)) as client:
                names = sorted(t.name for t in (await client.list_tools()).tools)
                first = await client.call_tool("list_accounts", {})
        # 앱이 포트를 9100으로 바꿔 다시 시작했다고 가정 — 프록시 재등록 없이 따라간다.
        app_b = env.components.build_asgi(9100)
        async with env.components.tool_server.session_manager.run():
            forwarder_b = ProxyForwarder(
                env.secret_store,
                handshake=lambda _secret: next(ports),
                http_client_factory=_factory(app_b, seen_urls),
            )
            async with Client(build_proxy_server(forwarder_b)) as client:
                await client.list_tools()
                second = await client.call_tool("list_accounts", {})
        return names, first.content[0].text, second.content[0].text

    try:
        names, first, second = asyncio.run(_run())
    finally:
        env.close()
    assert "create_draft" in names and "send_draft" not in names
    assert "me@example.com" in first and "me@example.com" in second
    assert seen_urls[0].endswith(":8765") and seen_urls[-1].endswith(":9100")


def _call_through(env: McpEnv, handshake: Any) -> Any:
    async def _run() -> Any:
        app = env.components.build_asgi(8765)
        async with env.components.tool_server.session_manager.run():
            forwarder = ProxyForwarder(
                env.secret_store, handshake=handshake, http_client_factory=_factory(app, [])
            )
            async with Client(build_proxy_server(forwarder)) as client:
                return await client.call_tool("list_accounts", {})

    return asyncio.run(_run())


def _raise(exc: Exception):  # noqa: ANN202
    def _h(_secret: bytes) -> int:
        raise exc

    return _h


def test_proxy_reports_app_not_running_fake_server_and_disabled(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _setup(env)
    try:
        not_running = _call_through(env, _raise(AppNotRunningError("EmailToMCP 앱을 실행하세요")))
        fake = _call_through(env, _raise(SecurityError("HMAC 실패")))
        disabled = _call_through(env, _raise(McpDisabledError("MCP 꺼짐")))
    finally:
        env.close()
    assert not_running.is_error and "실행" in not_running.content[0].text
    assert fake.is_error and "HMAC" in fake.content[0].text
    assert disabled.is_error and "꺼" in disabled.content[0].text


def test_proxy_without_secret_or_token_gives_setup_guidance(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        no_secret = _call_through(env, lambda _s: 8765)
        ensure_handshake_secret(env.secret_store)
        no_token = _call_through(env, lambda _s: 8765)
    finally:
        env.close()
    assert no_secret.is_error and "앱을 한 번 실행" in no_secret.content[0].text
    assert no_token.is_error and "프록시 토큰" in no_token.content[0].text


def test_proxy_wraps_unexpected_transport_failure_without_leaking_detail(tmp_path: Path) -> None:
    """핸드셰이크는 성공했지만 실제 중계(ASGI 호출) 중 예상 못한 예외가 나면, 원인 타입이 아니라
    고정된 안내 문구로만 응답한다(토큰 등 상세가 새지 않게)."""
    env = build_env(tmp_path)
    _setup(env)

    async def _broken_app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN001
        raise RuntimeError("boom: should not leak")

    try:
        result = _call_through_with_app(env, lambda _s: 8765, _broken_app)
    finally:
        env.close()
    assert result.is_error
    assert "통신하지 못했습니다" in result.content[0].text
    assert "boom" not in result.content[0].text


def _call_through_with_app(env: McpEnv, handshake: Any, app: Any) -> Any:
    async def _run() -> Any:
        forwarder = ProxyForwarder(
            env.secret_store, handshake=handshake, http_client_factory=_factory(app, [])
        )
        async with Client(build_proxy_server(forwarder)) as client:
            return await client.call_tool("list_accounts", {})

    return asyncio.run(_run())


def test_proxy_with_revoked_token_fails_without_leaking_token(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    _setup(env)
    token_id = env.components.tokens.list_tokens()[0].id
    env.components.tokens.issue(kind="http", scopes=["read"], label="other")
    env.components.tokens.revoke(token_id)
    try:
        result = _call_through(env, lambda _s: 8765)
    finally:
        env.close()
    assert result.is_error
    assert "Bearer" not in result.content[0].text
