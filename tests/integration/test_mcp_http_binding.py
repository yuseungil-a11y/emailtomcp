"""실제 uvicorn + 소켓 바인딩 (DESIGN.md §6.1 M2·L1·C-05, §12.3 P2 "포트 충돌 시 자동 변경 0회").

랜덤 포트(0)로 띄워 127.0.0.1에만 바인딩되는지, 공식 SDK 클라이언트가 실제 HTTP로
붙는지, 포트 충돌 시 다른 포트로 바꾸지 않고 실패하는지 확인한다.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from emailtomcp.runtime.asgi_host import AsgiHost, PortUnavailableError, create_loopback_socket
from mcp_support import MCP_HEADERS, build_env, jsonrpc_body

pytestmark = pytest.mark.integration


def test_real_server_binds_loopback_only_and_serves_sdk_client(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    token = env.token(["read"])

    async def _run() -> dict[str, object]:
        sock = create_loopback_socket(0)
        bound_host, port = sock.getsockname()[:2]
        host = AsgiHost(
            env.components.build_asgi(port),
            sock,
            lifespan=env.components.tool_server.session_manager.run,
        )
        await host.start()
        out: dict[str, object] = {"bound_host": bound_host, "running": host.running}
        try:
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=10.0
            ) as http_client:
                raw = await http_client.post(
                    f"http://127.0.0.1:{port}/mcp",
                    headers=MCP_HEADERS,
                    content=json.dumps(jsonrpc_body()).encode(),
                )
                out["raw_status"] = raw.status_code
                async with Client(
                    streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http_client)
                ) as client:
                    out["tools"] = sorted(t.name for t in (await client.list_tools()).tools)
            # IPv6 loopback(::1)에는 바인딩하지 않았다(L1).
            if socket.has_ipv6:
                probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                probe.settimeout(1.0)
                try:
                    out["ipv6_connect"] = probe.connect_ex(("::1", port))
                finally:
                    probe.close()
        finally:
            await host.stop()
        out["running_after_stop"] = host.running
        return out

    try:
        out = asyncio.run(_run())
    finally:
        env.close()
    assert out["bound_host"] == "127.0.0.1"
    assert out["running"] is True and out["running_after_stop"] is False
    assert out["raw_status"] == 200
    assert "get_message" in out["tools"] and "send_draft" not in out["tools"]  # type: ignore[operator]
    if "ipv6_connect" in out:
        assert out["ipv6_connect"] != 0


def test_port_conflict_fails_without_switching_port() -> None:
    first = create_loopback_socket(0)
    port = first.getsockname()[1]
    try:
        with pytest.raises(PortUnavailableError) as info:
            create_loopback_socket(port)
        assert info.value.port == port  # 다른 포트로 바꾸지 않았다
    finally:
        first.close()


@pytest.mark.skipif(sys.platform != "win32", reason="SO_EXCLUSIVEADDRUSE는 Windows 전용")
def test_exclusive_bind_blocks_reuseaddr_hijack() -> None:
    """M2: 다른 소켓이 SO_REUSEADDR로 같은 포트를 가로채 바인딩할 수 없다."""
    ours = create_loopback_socket(0)
    port = ours.getsockname()[1]
    attacker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        attacker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            attacker.bind(("127.0.0.1", port))
    finally:
        attacker.close()
        ours.close()


def test_on_exit_runs_before_lifespan_cleanup_and_only_once() -> None:
    """N-3: uvicorn이 멈추면 lifespan 정리(__aexit__)보다 먼저 on_exit가 불려 "실행 중"
    상태가 내려간다 — 정리가 끝날 때까지 닫히는 포트를 핸드셰이크가 알려 주지 않게."""
    events: list[str] = []

    class _Lifespan:
        async def __aenter__(self) -> None:
            events.append("enter")

        async def __aexit__(self, *exc: object) -> None:
            events.append("aexit")

    async def _app(scope, receive, send) -> None:  # noqa: ANN001
        return None

    async def _run() -> None:
        sock = create_loopback_socket(0)
        host = AsgiHost(_app, sock, lifespan=_Lifespan, on_exit=lambda: events.append("on_exit"))
        await host.start()
        await host.stop()

    asyncio.run(_run())
    assert events == ["enter", "on_exit", "aexit"]


def test_start_timeout_cancels_task_and_frees_socket_for_retry() -> None:
    """EMCP-2026-1008: lifespan 진입이 멈추면(예: 방화벽 정책 엔진 경합 등 바깥 요인)
    `start()`가 영원히 걸리는 대신 `timeout`초 안에 `TimeoutError`를 내고, 이미
    bind·listen된 소켓을 정리해 같은 포트로 재시도할 수 있게 한다."""

    class _HangingLifespan:
        async def __aenter__(self) -> None:
            await asyncio.Event().wait()  # 아무도 set하지 않는다 — 영원히 멈춘다

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def _app(scope, receive, send) -> None:  # noqa: ANN001
        return None

    async def _run() -> tuple[int, bool]:
        sock = create_loopback_socket(0)
        port = sock.getsockname()[1]
        host = AsgiHost(_app, sock, lifespan=_HangingLifespan)
        with pytest.raises(TimeoutError):
            await host.start(timeout=0.2)
        return port, host.running

    port, running = asyncio.run(_run())
    assert running is False
    # 소켓이 실제로 닫혔다 — 같은 포트로 바로 다시 바인딩할 수 있어야 한다(재시도 가능).
    retry = create_loopback_socket(port)
    retry.close()


def test_on_exit_runs_while_listening_socket_is_still_open() -> None:
    """S-1: on_exit 호출 시점에 리슨 소켓이 아직 열려 있다(닫힌 뒤가 아니다).

    예전 코드는 `uvicorn.Server.serve()`가 반환한 뒤(=소켓이 이미 닫힌 뒤)에야
    `on_exit`를 불렀다. 지금은 `_NotifyingServer.shutdown()`이 소켓을 닫기 전에
    먼저 알려 준다 — 콜백 안에서 같은 포트로 접속을 시도해 아직 열려 있는지
    확인한다.
    """
    still_open: list[bool] = []

    async def _app(scope, receive, send) -> None:  # noqa: ANN001
        return None

    async def _run() -> None:
        sock = create_loopback_socket(0)
        port = sock.getsockname()[1]

        def _on_exit() -> None:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                still_open.append(probe.connect_ex(("127.0.0.1", port)) == 0)
            finally:
                probe.close()

        host = AsgiHost(_app, sock, on_exit=_on_exit)
        await host.start()
        await host.stop()

    asyncio.run(_run())
    assert still_open == [True]
