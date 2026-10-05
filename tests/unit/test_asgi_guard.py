"""L1 ASGI 가드(`asgi_guard.AsgiGuard`/`ActivityTracker`) 직접 단위 테스트.

DESIGN.md §6.1 D-08·L1·M2·H4. 통합 테스트(`tests/integration/test_mcp_auth_matrix.py`)는
실제 MCP 클라이언트로 정상 경로를 확인하지만, lifespan/websocket 처리, 가드 콜백의
예외 흡수, 본문 스트리밍의 경계값(상한 초과·비정상 길이)은 가드를 직접 호출해야 쉽게
검증할 수 있다(갈릴레오 C2 커버리지 보강).

이 프로젝트는 pytest-asyncio를 쓰지 않으므로(다른 테스트 파일과 동일한 관례), 각 async
시나리오는 동기 테스트 함수 안에서 `asyncio.run()`으로 돌린다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from emailtomcp.core.clock import FakeClock
from emailtomcp.core.events import EventBus
from emailtomcp.core.models import McpAuditPrincipal
from emailtomcp.mcp_server.asgi_guard import (
    EVENT_MCP_CLIENT_CONNECTED,
    EVENT_MCP_CLIENT_DISCONNECTED,
    ActivityTracker,
    AsgiGuard,
)
from emailtomcp.mcp_server.auth import Principal

pytestmark = pytest.mark.unit


def _scope(
    *, method: str = "POST", path: str = "/mcp", headers: dict[str, str] | None = None
) -> dict[str, Any]:
    raw = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    return {"type": "http", "method": method, "path": path, "headers": raw}


DEFAULT_HEADERS = {
    "content-type": "application/json",
    "host": "127.0.0.1:8765",
    "authorization": "Bearer good",
}


class _Recorder:
    """ASGI receive/send를 흉내낸다. `inbox`를 순서대로 돌려주고, 바닥나면 disconnect."""

    def __init__(self, inbox: list[dict[str, Any]] | None = None) -> None:
        self._inbox = list(inbox or [])
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        if self._inbox:
            return self._inbox.pop(0)
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


async def _authenticate_good(token: str) -> Principal | None:
    if token == "good":
        return Principal(kind=McpAuditPrincipal.INTERACTIVE, scopes=frozenset({"read"}), token_id=1)
    return None


async def _echo_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    received: list[dict[str, Any]] = []
    while True:
        msg = await receive()
        received.append(msg)
        if not msg.get("more_body", False):
            break
    scope["state"]["echo_received"] = received
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _status_of(recorder: _Recorder) -> int:
    start = next(m for m in recorder.sent if m["type"] == "http.response.start")
    return start["status"]


def _run_guard(guard: AsgiGuard, scope: dict[str, Any], recorder: _Recorder) -> None:
    asyncio.run(guard(scope, recorder.receive, recorder.send))


# ---------------------------------------------------------------- lifespan/websocket


def test_lifespan_scope_is_acknowledged_without_running_inner_app() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    recorder = _Recorder([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    _run_guard(guard, {"type": "lifespan"}, recorder)
    assert recorder.sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


def test_websocket_scope_is_closed() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    recorder = _Recorder()
    _run_guard(guard, {"type": "websocket"}, recorder)
    assert recorder.sent == [{"type": "websocket.close", "code": 1008}]


# ---------------------------------------------------------------- 거부 경로


def test_wrong_method_denied_with_allow_header() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    recorder = _Recorder()
    _run_guard(guard, _scope(method="GET", headers=DEFAULT_HEADERS), recorder)
    assert _status_of(recorder) == 405
    start = recorder.sent[0]
    assert (b"allow", b"POST") in start["headers"]


def test_wrong_path_denied() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    recorder = _Recorder()
    _run_guard(guard, _scope(path="/other", headers=DEFAULT_HEADERS), recorder)
    assert _status_of(recorder) == 404


def test_missing_content_type_denied() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    headers = dict(DEFAULT_HEADERS)
    del headers["content-type"]
    recorder = _Recorder()
    _run_guard(guard, _scope(headers=headers), recorder)
    assert _status_of(recorder) == 415


def test_invalid_content_length_denied_400() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    headers = {**DEFAULT_HEADERS, "content-length": "not-a-number"}
    recorder = _Recorder()
    _run_guard(guard, _scope(headers=headers), recorder)
    assert _status_of(recorder) == 400


def test_declared_content_length_too_large_denied_413() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good, max_body_bytes=10)
    headers = {**DEFAULT_HEADERS, "content-length": "999"}
    recorder = _Recorder()
    _run_guard(guard, _scope(headers=headers), recorder)
    assert _status_of(recorder) == 413


def test_wrong_host_denied_403() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    headers = {**DEFAULT_HEADERS, "host": "evil.example:8765"}
    recorder = _Recorder()
    _run_guard(guard, _scope(headers=headers), recorder)
    assert _status_of(recorder) == 403


def test_origin_present_denied_403() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    headers = {**DEFAULT_HEADERS, "origin": "null"}
    recorder = _Recorder()
    _run_guard(guard, _scope(headers=headers), recorder)
    assert _status_of(recorder) == 403


def test_bad_token_denied_401() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    headers = {**DEFAULT_HEADERS, "authorization": "Bearer wrong"}
    recorder = _Recorder()
    _run_guard(guard, _scope(headers=headers), recorder)
    assert _status_of(recorder) == 401


# ---------------------------------------------------------------- on_denied/on_authenticated 콜백


def test_on_denied_hook_exception_is_swallowed_and_response_still_sent() -> None:
    async def _boom(_status: int, _reason: str) -> None:
        raise RuntimeError("audit down")

    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good, on_denied=_boom)
    recorder = _Recorder()
    _run_guard(guard, _scope(method="GET", headers=DEFAULT_HEADERS), recorder)
    assert _status_of(recorder) == 405  # 훅이 터져도 응답은 정상적으로 나간다


def test_on_authenticated_hook_exception_is_swallowed_and_app_still_runs() -> None:
    def _boom(_principal: Principal) -> None:
        raise RuntimeError("tracker down")

    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good, on_authenticated=_boom)
    recorder = _Recorder([{"type": "http.request", "body": b"{}", "more_body": False}])
    _run_guard(guard, _scope(headers=DEFAULT_HEADERS), recorder)
    assert _status_of(recorder) == 200


# ---------------------------------------------------------------- 본문 스트리밍


def test_successful_flow_streams_multi_chunk_body_and_sets_principal() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good)
    recorder = _Recorder(
        [
            {"type": "http.request", "body": b'{"a":', "more_body": True},
            {"type": "http.request", "body": b"1}", "more_body": False},
        ]
    )
    scope = _scope(headers=DEFAULT_HEADERS)
    _run_guard(guard, scope, recorder)
    assert _status_of(recorder) == 200
    assert isinstance(scope["state"]["principal"], Principal)
    received = scope["state"]["echo_received"]
    assert b"".join(m["body"] for m in received) == b'{"a":1}'


def test_streaming_body_without_content_length_over_limit_denied_413() -> None:
    guard = AsgiGuard(_echo_app, port=8765, authenticate=_authenticate_good, max_body_bytes=5)
    recorder = _Recorder([{"type": "http.request", "body": b"123456", "more_body": False}])
    _run_guard(guard, _scope(headers=DEFAULT_HEADERS), recorder)
    assert _status_of(recorder) == 413


def test_trailing_non_request_message_is_replayed_to_app() -> None:
    """본문 수신 중 `http.request`가 아닌 메시지(예: disconnect)가 오면 멈추고, 그 메시지도
    앱에 그대로 전달한다(재생 목록에 포함)."""

    async def _app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        msgs = [await receive(), await receive()]
        scope["state"]["app_msgs"] = msgs
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guard = AsgiGuard(_app, port=8765, authenticate=_authenticate_good)
    recorder = _Recorder(
        [
            {"type": "http.request", "body": b"{}", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )
    scope = _scope(headers=DEFAULT_HEADERS)
    _run_guard(guard, scope, recorder)
    assert _status_of(recorder) == 200
    msgs = scope["state"]["app_msgs"]
    assert msgs[0] == {"type": "http.request", "body": b"{}", "more_body": False}
    assert msgs[1] == {"type": "http.disconnect"}


# ---------------------------------------------------------------- ActivityTracker


def test_activity_tracker_ignores_non_interactive_principal() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    bus = EventBus()
    events: list[tuple[str, object]] = []
    bus.subscribe("*", lambda name, payload: events.append((name, payload)))
    tracker = ActivityTracker(bus, clock)

    job_principal = Principal(kind=McpAuditPrincipal.JOB, scopes=frozenset(), job_id=1)
    tracker.touch(job_principal)
    assert tracker.connected is False
    assert tracker.last_activity_at() is None
    assert events == []


def test_activity_tracker_connects_once_and_reports_idle() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    bus = EventBus()
    events: list[tuple[str, object]] = []
    bus.subscribe("*", lambda name, payload: events.append((name, payload)))
    tracker = ActivityTracker(bus, clock, idle_seconds=10.0)

    principal = Principal(
        kind=McpAuditPrincipal.INTERACTIVE, scopes=frozenset({"read"}), token_id=1, label="t"
    )
    tracker.touch(principal)
    assert tracker.connected is True
    assert tracker.last_activity_at() == 0.0
    assert [e[0] for e in events] == [EVENT_MCP_CLIENT_CONNECTED]

    # 두 번째 touch는 이미 연결 상태이므로 Connected를 다시 발행하지 않는다.
    clock.advance(5)
    tracker.touch(principal)
    assert [e[0] for e in events] == [EVENT_MCP_CLIENT_CONNECTED]

    # 유휴 시간이 지나지 않으면 False.
    assert tracker.check_idle() is False

    clock.advance(11)
    assert tracker.check_idle() is True
    assert tracker.connected is False
    assert tracker.last_activity_at() is None
    assert [e[0] for e in events] == [EVENT_MCP_CLIENT_CONNECTED, EVENT_MCP_CLIENT_DISCONNECTED]

    # 이미 끊긴 상태에서 다시 확인하면 False.
    assert tracker.check_idle() is False
