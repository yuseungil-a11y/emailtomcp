"""MCP 서버 수명 관리(app.McpServiceRunner) — 포트 충돌 시 자동 변경 0회·경고 이벤트(C-05),
정상 시작/종료와 McpStatusChanged 발행(§4.2), --mcp-port 해석(§4.6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from emailtomcp.app import DEFAULT_MCP_PORT, McpServiceRunner, _resolve_mcp_port
from emailtomcp.runtime.asgi_host import create_loopback_socket
from mcp_support import build_env

pytestmark = pytest.mark.integration


def _status_events(env) -> list[dict]:  # noqa: ANN001
    return [p for name, p in env.events if name == "McpStatusChanged"]


def test_runner_port_conflict_keeps_mcp_off_without_switching(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    blocker = create_loopback_socket(0)
    port = blocker.getsockname()[1]
    try:
        runner = McpServiceRunner(env.components, env.event_bus, port=port)
        asyncio.run(runner.start())
        snap = runner.snapshot()
        events = _status_events(env)
    finally:
        blocker.close()
        env.close()
    assert snap["running"] is False and snap["port"] == port and snap["error"]
    assert runner.current_port() is None  # 핸드셰이크는 "MCP 비활성"으로 응답한다
    assert len(events) == 1 and events[0]["port"] == port and events[0]["error"]


def test_runner_start_and_stop_publish_status(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    probe = create_loopback_socket(0)
    port = probe.getsockname()[1]
    probe.close()

    async def _run() -> tuple[dict, int | None, dict]:
        runner = McpServiceRunner(env.components, env.event_bus, port=port)
        await runner.start()
        running = runner.snapshot()
        current = runner.current_port()
        await runner.stop()
        return running, current, runner.snapshot()

    try:
        running, current, stopped = asyncio.run(_run())
        events = _status_events(env)
    finally:
        env.close()
    assert running == {"running": True, "port": port, "error": None}
    assert current == port
    assert stopped["running"] is False
    assert [e["running"] for e in events] == [True, False]


def test_current_port_is_none_when_host_dies_unexpectedly(tmp_path: Path) -> None:
    """보안검토 M-3: uvicorn이 예기치 않게 끝나면 current_port()는 즉시 None, 상태도 하강."""
    env = build_env(tmp_path)
    probe = create_loopback_socket(0)
    port = probe.getsockname()[1]
    probe.close()

    async def _run() -> tuple[int | None, int | None, dict, int | None]:
        runner = McpServiceRunner(env.components, env.event_bus, port=port)
        await runner.start()
        before = runner.current_port()
        host = runner._host
        assert host is not None
        # stop()을 거치지 않고 서버만 끝낸다(비정상 종료 흉내).
        host._server.should_exit = True  # type: ignore[union-attr]
        await asyncio.wait_for(host._task, timeout=10)  # type: ignore[arg-type]
        after = runner.current_port()
        snap = runner.snapshot()
        await runner.stop()  # 이미 내려간 상태 — 중복 이벤트 없이 끝나야 한다
        return before, after, snap, runner.current_port()

    try:
        before, after, snap, final = asyncio.run(_run())
        events = _status_events(env)
    finally:
        env.close()
    assert before == port
    assert after is None and final is None
    assert snap["running"] is False and snap["error"]
    assert [e["running"] for e in events] == [True, False]


def test_current_port_checks_host_liveness_not_only_flag(tmp_path: Path) -> None:
    """M-3: 상태 플래그가 True로 남아 있어도 호스트가 죽었으면 None."""
    env = build_env(tmp_path)

    class _DeadHost:
        running = False
        port = 1234

    try:
        runner = McpServiceRunner(env.components, env.event_bus, port=1234)
        runner._state = {"running": True, "port": 1234, "error": None}
        runner._host = _DeadHost()  # type: ignore[assignment]
        assert runner.current_port() is None
    finally:
        env.close()


def test_resolve_mcp_port() -> None:
    assert _resolve_mcp_port(None, None) == DEFAULT_MCP_PORT
    assert _resolve_mcp_port(None, 9000) == 9000
    assert _resolve_mcp_port(None, "bad") == DEFAULT_MCP_PORT
    assert _resolve_mcp_port(None, 80) == DEFAULT_MCP_PORT  # 1024 미만 거부
    assert _resolve_mcp_port(9100, 9000) == 9100
