"""QtBridge — core.events 버스를 Qt Signal로 재발행하는 유일한 Qt-aware 어댑터.

서비스(backend 쪽)는 이 클래스를 알지 못한다 — 항상 `core.events.EventBus`로만 발행한다
(소크라테스 §1.2 "services는 core만 알고 runtime은 모른다"). UI가 백엔드 코루틴을 호출할 때는
`call_backend()`를 거친다 — `run_coroutine_threadsafe` 호출과 예외 처리를 한 곳에 모은다
(소크라테스 §3.3).
"""

from __future__ import annotations

import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

from PySide6.QtCore import QObject, Signal

from emailtomcp.core.events import EventBus
from emailtomcp.runtime.backend import Backend

logger = logging.getLogger(__name__)

T = TypeVar("T")


class QtBridge(QObject):
    """event_received(event_name, payload) Signal로 core.events를 재발행한다."""

    event_received = Signal(str, object)

    def __init__(
        self, backend: Backend, event_bus: EventBus, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._event_bus = event_bus
        self._event_bus.subscribe("*", self._on_event)

    def _on_event(self, name: str, payload: object) -> None:
        self.event_received.emit(name, payload)

    def call_backend(self, coro: Coroutine[Any, Any, T], timeout: float | None = 5.0) -> T:
        """Qt 스레드에서 백엔드 코루틴을 동기적으로 호출한다. 예외는 로그 남기고 그대로 전달한다."""
        try:
            return self._backend.submit(coro, timeout=timeout)
        except Exception:
            logger.exception("call_backend 실패")
            raise
