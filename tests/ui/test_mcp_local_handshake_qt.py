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


# ---------------------------------------------------------------- M-1/M-2(데카르트 QA #48)


def test_korean_usernames_do_not_collide(monkeypatch: pytest.MonkeyPatch) -> None:
    """한글 계정명 2개는 서로 다른 해시 키를 만들어야 한다(M-1 — 과거엔 둘 다 'kim'으로 충돌)."""
    monkeypatch.setattr(hs, "_current_user_identity", lambda: "kim철수")
    token_a = hs._current_user_token()
    monkeypatch.setattr(hs, "_current_user_identity", lambda: "kim영희")
    token_b = hs._current_user_token()

    assert token_a is not None
    assert token_b is not None
    assert token_a != token_b
    # 과거처럼 비한글 접두만 남는 식으로 퇴화하지 않았는지 재확인.
    assert "kim" not in token_a and "kim" not in token_b


def test_identity_lookup_failure_falls_back_to_common_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사용자 식별값을 전혀 얻지 못하면(OS API·폴백 모두 실패) 안전하게 공용 이름으로 내려간다."""
    monkeypatch.setattr(hs, "_current_user_identity", lambda: None)
    assert hs._current_user_token() is None
    assert hs._single_instance_key() == hs._BASE_SINGLE_INSTANCE_KEY


def test_different_env_vars_still_yield_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """GUI와 stdio 프록시가 서로 다른 환경변수를 가져도 같은 키를 계산해야 한다(M-2 회귀 수정).

    OS API 기반 조회(`_current_user_identity`)는 바꾸지 않고, 환경변수만 양쪽에서 다르게
    흉내 낸다 — 환경변수가 더 이상 키 계산에 영향을 주면 안 된다.
    """
    monkeypatch.setattr(hs, "_current_user_identity", lambda: "현재사용자")

    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    key_gui = hs._single_instance_key()

    monkeypatch.setenv("USERNAME", "완전히다른값")
    monkeypatch.setenv("USER", "completely-different")
    key_stdio_proxy = hs._single_instance_key()

    assert key_gui == key_stdio_proxy
