"""실제 QLocalServer(앱의 SingleInstanceGuard) ↔ 프록시 핸드셰이크(표준 라이브러리 클라이언트)
(DESIGN.md §6.4, §2(a) L2, §12.3 P2 "프록시: 가짜 서버 거부").

Windows에서는 이름 있는 파이프, 그 밖의 OS에서는 유닉스 소켓 경로를 프록시가 Qt 없이
직접 계산해 접속하므로, 실제 QLocalServer와의 호환을 이 테스트로 확인한다.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest

from emailtomcp.app import SingleInstanceGuard
from emailtomcp.core.errors import SecurityError
from emailtomcp.mcp_server import local_handshake as hs

pytestmark = pytest.mark.ui


def _run_in_thread(fn):  # noqa: ANN001, ANN202
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, box


@pytest.fixture
def guard(qtbot, tmp_path: Path, monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setenv("EMAILTOMCP_DATA_DIR", str(tmp_path))
    key = f"EmailToMCP-test-{uuid.uuid4().hex[:12]}"
    secret = b"k" * 32
    state: dict[str, object] = {"activated": 0, "port": 8765}

    def _activate() -> None:
        state["activated"] = int(state["activated"]) + 1  # type: ignore[arg-type]

    def _handshake(nonce_hex: str) -> bytes:
        return hs.build_response(secret, nonce_hex, state["port"])  # type: ignore[arg-type]

    instance = SingleInstanceGuard(key)
    assert instance.try_acquire(_activate, _handshake)
    yield instance, key, secret, state
    instance.release()


def test_real_local_server_handshake_returns_verified_port(qtbot, guard) -> None:  # noqa: ANN001
    _instance, key, secret, _state = guard
    thread, box = _run_in_thread(lambda: hs.client_handshake(secret, server_name=key, timeout=5.0))
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=8000)
    assert box.get("error") is None, box.get("error")
    assert box["value"] == 8765


def test_proxy_rejects_server_with_different_secret(qtbot, guard) -> None:  # noqa: ANN001
    _instance, key, _secret, _state = guard
    thread, box = _run_in_thread(
        lambda: hs.client_handshake(b"z" * 32, server_name=key, timeout=5.0)
    )
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=8000)
    assert isinstance(box.get("error"), SecurityError)


def test_mcp_disabled_is_reported(qtbot, guard) -> None:  # noqa: ANN001
    _instance, key, secret, state = guard
    state["port"] = None
    thread, box = _run_in_thread(lambda: hs.client_handshake(secret, server_name=key, timeout=5.0))
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=8000)
    assert isinstance(box.get("error"), hs.McpDisabledError)


def test_activate_and_unknown_commands(qtbot, guard) -> None:  # noqa: ANN001
    _instance, key, _secret, state = guard
    exchange = hs._exchange_windows if hs.sys.platform == "win32" else hs._exchange_unix
    path = hs.local_server_path(key)
    thread, box = _run_in_thread(lambda: exchange(path, b"activate\n", 5.0))
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=8000)
    qtbot.waitUntil(lambda: state["activated"] == 1, timeout=3000)
    thread2, box2 = _run_in_thread(lambda: exchange(path, b"shutdown\n", 5.0))
    qtbot.waitUntil(lambda: not thread2.is_alive(), timeout=8000)
    # 허용 외 명령에는 아무 응답도 하지 않는다(L2).
    assert box2.get("value", b"") == b""
    assert state["activated"] == 1
