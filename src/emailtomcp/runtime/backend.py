"""Backend — 전용 스레드에서 asyncio 이벤트 루프를 하나 돌리는 런타임.

DESIGN.md §2.a 스레드 구조:
- Qt 메인 스레드는 UI만 담당한다.
- Backend 스레드는 asyncio 루프를 하나 돌린다. 이 루프 위에서 uvicorn(MCP, P2),
  수신 스케줄러(P1), 자동회신 잡 러너(P3)가 동작한다.
- 블로킹 I/O는 크기를 제한한 전용 executor로 돌린다(소크라테스 §3.1 — 기본 executor의
  워커 최대 32개를 그대로 쓰면 SQLite 연결이 난립하고 busy 경합이 생긴다).
- qasync는 쓰지 않는다. UI → 백엔드 호출은 `run_coroutine_threadsafe`로 한다.

이 모듈은 PySide6를 import하지 않는다(아키텍처 테스트로 강제, tests/unit/
test_architecture_no_qt_in_backend.py). Qt와 연결하는 어댑터는 `runtime.qt_bridge`에 있다.

종료 순서(소크라테스 §3.4, P0): 새 작업 수락 중단 → (실행 중인 서비스 정리는 상위 서비스가
개별적으로 담당, P0은 서비스가 없음) → 루프 stop → 스레드 join → executor 종료.
DB writer 종료와 락 해제는 이 클래스 바깥(app.py)에서 이어서 수행한다.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_EXECUTOR_WORKERS = 4


class Backend:
    """asyncio 이벤트 루프를 전용 스레드에서 돌리는 백엔드 런타임."""

    def __init__(self, *, max_workers: int = DEFAULT_EXECUTOR_WORKERS) -> None:
        self._max_workers = max_workers
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._ready = threading.Event()
        self._accepting_jobs = False
        self._lock = threading.Lock()
        self._periodic_tasks: list[asyncio.Task[None]] = []

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("Backend가 시작되지 않았습니다")
        return self._loop

    def start(self) -> None:
        """백엔드 스레드를 시작하고 루프가 준비될 때까지 기다린다."""
        if self._thread is not None:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="emailtomcp-backend", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("Backend 루프가 5초 안에 준비되지 않았습니다")
        with self._lock:
            self._accepting_jobs = True

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(self._handle_loop_exception)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="emailtomcp-io"
        )
        loop.set_default_executor(self._executor)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    @staticmethod
    def _handle_loop_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        """루프 자체가 잡아낸(태스크에서 끝까지 전파된) 처리되지 않은 예외를 로그에 남긴다
        (디버깅 로그 요구사항). asyncio 기본 처리기는 stderr에만 찍고 지나가므로,
        여기서 받아 앱 로그 파일에도 남긴다."""
        del loop
        message = context.get("message", "백엔드 이벤트 루프에서 처리되지 않은 예외")
        exc = context.get("exception")
        if exc is not None:
            logger.error(message, exc_info=exc)
        else:
            logger.error("%s: %s", message, context)

    def submit(self, coro: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
        """외부(Qt 스레드 등)에서 호출: 코루틴을 백엔드 루프에서 실행하고 결과를 기다린다."""
        future = self.submit_async(coro)
        return future.result(timeout=timeout)

    def submit_async(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """코루틴을 백엔드 루프에 예약하고 즉시 반환한다(블로킹하지 않음).

        연결 테스트·수신·발송처럼 수 초 걸릴 수 있는 호출에서 Qt 메인 스레드를 막지
        않으려는 호출자가 쓴다. 완료 여부는 반환된 Future를 `QTimer` 등으로 폴링한다.
        """
        with self._lock:
            accepting = self._accepting_jobs
        if not accepting:
            coro.close()  # "coroutine was never awaited" 경고 방지
            raise RuntimeError("Backend가 새 작업을 받지 않는 상태입니다(시작 전 또는 종료 중)")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def schedule_periodic(
        self,
        fn: Callable[[], object],
        *,
        initial_delay: float,
        interval: float,
        name: str,
    ) -> None:
        """블로킹 함수 `fn`을 `initial_delay`초 뒤부터 `interval`초마다 executor에서 실행한다.

        업데이트 확인(§3.4: 기동 30초 뒤, 이후 주기) 같은 주기 작업용 범용 훅이다 — Backend는
        서비스 로직을 모르므로(§4.2) 무엇을 실행할지는 app.py가 넘긴다. `fn`의 예외는 로그만
        남기고 다음 주기를 계속한다. 등록한 태스크는 `stop()`이 루프를 멈추기 전에 취소한다.
        """
        if self._loop is None:
            raise RuntimeError("Backend가 시작되지 않았습니다")

        async def _runner() -> None:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(initial_delay)
            while True:
                try:
                    await loop.run_in_executor(None, fn)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — 주기 작업 1회 실패가 루프를 죽이면 안 됨
                    logger.exception("주기 작업 실패: %s", name)
                await asyncio.sleep(interval)

        def _create() -> None:
            task = self.loop.create_task(_runner(), name=name)
            self._periodic_tasks.append(task)

        self._loop.call_soon_threadsafe(_create)

    def _cancel_periodic_tasks(self) -> None:
        for task in self._periodic_tasks:
            task.cancel()
        self._periodic_tasks.clear()

    def call_soon_threadsafe(self, callback) -> None:  # noqa: ANN001 — 콜백 시그니처는 호출자 책임
        """루프가 떠 있을 때만 `callback`을 예약한다(종료 경로 등에서 안전하게 쓴다)."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(callback)

    def stop(self, timeout: float | None = 10.0) -> None:
        """종료 순서: 1) 새 작업 수락 중단 2) 루프 stop 3) 스레드 join 4) executor 종료."""
        with self._lock:
            self._accepting_jobs = False
        if self._loop is None or self._thread is None:
            return
        # 주기 작업(schedule_periodic)은 루프를 멈추기 전에 취소한다("Task was destroyed" 방지).
        self._loop.call_soon_threadsafe(self._cancel_periodic_tasks)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        self._thread = None
        self._loop = None
