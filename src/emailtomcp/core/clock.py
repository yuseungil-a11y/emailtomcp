"""시간 의존 로직을 테스트 가능하게 만드는 Clock 추상화 (갈릴레오 §0 DI 포인트).

쿨다운 24h, 레이트리밋, 재시도 백오프, 승인 대기 120초 등은 모두 이 `Clock`을
주입받아 계산해야 한다. 테스트에서는 `FakeClock.advance()`로 시간을 즉시 넘긴다.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """타임존 포함 현재 시각(UTC 권장)."""
        ...

    def monotonic(self) -> float:
        """단조 증가 시계. 경과시간 측정(타임아웃 등)에 쓴다."""
        ...

    def sleep(self, seconds: float) -> None:
        """대기. 실제 구현은 블로킹이고, FakeClock은 기록만 한다."""
        ...


class SystemClock:
    """실제 시계. 운영 환경에서 쓴다."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """테스트용 시계. `advance()`로 now()/monotonic()을 수동으로 전진시킨다."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2026, 1, 1, tzinfo=UTC)
        self._monotonic = 0.0
        self.sleep_calls: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        # 실제로 기다리지 않는다. 필요하면 테스트에서 advance()를 함께 호출한다.
        self.sleep_calls.append(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds
