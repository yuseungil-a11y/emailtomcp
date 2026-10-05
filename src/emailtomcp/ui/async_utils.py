"""Qt 메인 스레드를 막지 않고 백엔드 코루틴을 기다리는 공용 헬퍼.

`qt_bridge.call_backend_async()`가 돌려주는 `concurrent.futures.Future`를 `QTimer`로
폴링한다 — 연결 테스트·수신·발송처럼 네트워크 I/O가 섞여 수 초 걸릴 수 있는 호출에서
Qt 메인 스레드(UI)를 막지 않기 위해 쓴다. 로컬 DB만 건드리는 짧은 호출은 그냥
`bridge.call_backend()`(동기)를 쓰면 된다.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from PySide6.QtCore import QObject, QTimer

# 완료 전에 QTimer가 GC되지 않도록 모듈 전역에서 참조를 붙잡아 둔다.
_KEEPALIVE: list[QTimer] = []


class _BridgeLike(Protocol):
    def call_backend_async(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future: ...


def run_async(
    bridge: _BridgeLike,
    coro: Coroutine[Any, Any, Any],
    on_done: Callable[[concurrent.futures.Future], None],
    *,
    parent: QObject | None = None,
    interval_ms: int = 200,
) -> None:
    """`coro`를 백엔드에 예약하고, 완료되면 Qt 메인 스레드에서 `on_done(future)`를 부른다."""
    future = bridge.call_backend_async(coro)
    timer = QTimer(parent)

    def _check() -> None:
        if not future.done():
            return
        timer.stop()
        timer.deleteLater()
        if timer in _KEEPALIVE:
            _KEEPALIVE.remove(timer)
        on_done(future)

    timer.timeout.connect(_check)
    _KEEPALIVE.append(timer)
    timer.start(interval_ms)
