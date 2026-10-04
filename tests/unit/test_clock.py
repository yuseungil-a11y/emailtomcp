from __future__ import annotations

from datetime import timedelta

from emailtomcp.core.clock import FakeClock, SystemClock


def test_system_clock_now_is_timezone_aware() -> None:
    clock = SystemClock()
    assert clock.now().tzinfo is not None


def test_system_clock_monotonic_increases() -> None:
    clock = SystemClock()
    first = clock.monotonic()
    second = clock.monotonic()
    assert second >= first


def test_fake_clock_advance_moves_now_and_monotonic() -> None:
    clock = FakeClock()
    start_now = clock.now()
    start_monotonic = clock.monotonic()

    clock.advance(10)

    assert clock.now() == start_now + timedelta(seconds=10)
    assert clock.monotonic() == start_monotonic + 10


def test_fake_clock_sleep_does_not_block_but_records_calls() -> None:
    clock = FakeClock()
    clock.sleep(5)
    clock.sleep(1.5)
    assert clock.sleep_calls == [5, 1.5]
