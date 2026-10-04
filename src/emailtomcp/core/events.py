"""백엔드 내부 이벤트 버스 (스레드 안전).

서비스는 이 버스로만 이벤트를 발행한다 — "services는 core만 알고 runtime은 모른다"
(소크라테스 §1.2). `runtime.qt_bridge.QtBridge`가 이 버스를 구독해 Qt Signal로
다시 내보내는 어댑터 역할을 한다. mcp_server ↔ autoreply처럼 서로 직접 참조하면
순환 의존이 생기는 패키지 쌍도 이 버스를 경유해 느슨하게 연결한다(소크라테스 §1.1).
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, object], None]

WILDCARD = "*"


class EventBus:
    """단순 pub-sub. `publish`는 호출한 스레드에서 핸들러를 동기 실행한다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        """특정 이벤트(또는 모든 이벤트: `"*"`)를 구독한다."""
        with self._lock:
            self._subscribers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        with self._lock:
            handlers = self._subscribers.get(event_name)
            if handlers and handler in handlers:
                handlers.remove(handler)

    def publish(self, event_name: str, payload: object = None) -> None:
        """이벤트를 발행한다. 핸들러 하나가 예외를 던져도 나머지 핸들러는 계속 실행된다."""
        with self._lock:
            handlers = [
                *self._subscribers.get(event_name, ()),
                *self._subscribers.get(WILDCARD, ()),
            ]
        for handler in handlers:
            try:
                handler(event_name, payload)
            except Exception:  # noqa: BLE001 — 핸들러 하나의 오류가 전체를 막으면 안 된다
                logger.exception("이벤트 핸들러 실행 실패: event=%s", event_name)
