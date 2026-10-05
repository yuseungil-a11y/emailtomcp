"""Backend.schedule_periodic — 업데이트 확인 같은 주기 작업 훅(§3.4)."""

from __future__ import annotations

import threading

from emailtomcp.runtime.backend import Backend


def test_periodic_task_runs_repeatedly_and_survives_exceptions() -> None:
    backend = Backend()
    backend.start()
    calls: list[int] = []
    reached = threading.Event()

    def _fn() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("첫 실행 실패")  # 다음 주기는 계속돼야 한다
        if len(calls) >= 3:
            reached.set()

    try:
        backend.schedule_periodic(_fn, initial_delay=0.01, interval=0.01, name="t")
        assert reached.wait(timeout=3.0)
    finally:
        backend.stop()
    count = len(calls)
    assert count >= 3


def test_stop_cancels_periodic_tasks() -> None:
    backend = Backend()
    backend.start()
    calls: list[int] = []
    backend.schedule_periodic(lambda: calls.append(1), initial_delay=60, interval=60, name="t")
    backend.stop()
    assert calls == []
    assert backend._periodic_tasks == []
