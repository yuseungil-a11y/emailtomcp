"""`SingleInstanceGuard`가 `QLocalServer.listen()` 실패를 확인·처리하는지(보안검토 P2 L-1).

"두 프로세스가 거의 동시에 실행됐을 때 하나만 남는지"의 실제 다중 프로세스 재현은
`tests/integration/test_single_instance_guard.py`가 맡는다. 여기서는 listen()이
실패하는 상황(권한 문제·OS 리소스 고갈 등) 자체를, 같은 프로세스 안에서 결정적으로
흉내 내 앱이 크래시 없이 계속 기동하는지(가용성 우선, L-1 권고) 확인한다.

(조사 메모) 같은 이름으로 또 다른 `QLocalServer`가 이미 `listen()` 중인 상태를 실제로
만들어 "이름 충돌"을 재현해 봤으나, 이 PC의 Windows 이름 있는 파이프는 같은 이름으로
여러 서버 인스턴스가 동시에 `listen()`할 수 있어(멀티 인스턴스 파이프) 충돌이 나지
않았다 — 즉 listen() 실패는 이름 충돌보다는 권한·리소스 문제 쪽에 가깝다. 그래서 여기서는
`QLocalServer.listen`을 몽키패치해 실패를 직접 강제한다.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
from PySide6.QtNetwork import QLocalServer

from emailtomcp.app import SingleInstanceGuard

pytestmark = pytest.mark.ui


def test_try_acquire_keeps_app_running_when_listen_fails(
    qtbot,  # noqa: ANN001 — pytest-qt 픽스처
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = f"EmailToMCP-test-listen-fail-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("EMAILTOMCP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(QLocalServer, "listen", lambda self, name: False)  # noqa: ARG005

    guard = SingleInstanceGuard(key)
    try:
        with caplog.at_level(logging.WARNING, logger="emailtomcp.app"):
            # 우리 쪽 데이터 디렉터리는 비어 있으므로 QLockFile.tryLock()은 성공한다 —
            # 그런데도 listen()은 (몽키패치로) 실패해야 한다.
            acquired = guard.try_acquire(lambda *_args: None)

        assert acquired is True, "listen() 실패해도 앱 기동 자체는 막지 않아야 한다(가용성 우선)"
        assert guard._server is None, "listen 실패를 인지하고 서버 참조를 정리해야 한다"
        assert any("바인딩에 실패" in record.message for record in caplog.records), (
            "listen() 실패를 경고 로그로 남겨야 한다"
        )
    finally:
        guard.release()


def test_try_acquire_second_guard_with_same_key_detects_existing_instance(
    qtbot,  # noqa: ANN001
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정상 경로(listen 성공) 대조군: 같은 키의 두 번째 guard는 활성화 요청만 보내고 False."""
    key = f"EmailToMCP-test-normal-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("EMAILTOMCP_DATA_DIR", str(tmp_path))

    first = SingleInstanceGuard(key)
    activated = {"count": 0}

    def _on_activate(*_args: object) -> None:
        activated["count"] += 1

    try:
        assert first.try_acquire(_on_activate)
        assert first._server is not None

        second = SingleInstanceGuard(key)
        try:
            assert second.try_acquire(lambda *_args: None) is False
        finally:
            second.release()

        qtbot.wait(300)
        assert activated["count"] == 1
    finally:
        first.release()
