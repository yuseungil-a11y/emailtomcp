"""SQLite 연결 팩토리와 단일 writer.

설계 근거(DESIGN.md §2.a, 소크라테스 §3.1):
- 연결 설정은 `PRAGMA journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`.
- 쓰기는 전용 스레드(writer) 하나로 직렬화한다. `asyncio.to_thread`의 기본 executor
  (워커 최대 32개)를 쓰면 SQLite 연결이 난립하고 busy 경합이 생기므로 쓰지 않는다.
- 읽기는 호출하는 스레드마다 별도 연결(`new_read_connection`)을 연다.
- 트랜잭션은 수동으로 제어한다(`isolation_level = None`) — PRAGMA/DDL/DML을 하나의
  `BEGIN ... COMMIT`으로 명확히 묶기 위함이다.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

BUSY_TIMEOUT_MS = 5000

_SENTINEL = object()


def configure_connection(conn: sqlite3.Connection) -> None:
    """연결 하나에 공통 PRAGMA를 적용한다."""
    conn.isolation_level = None  # 수동 트랜잭션 제어(BEGIN/COMMIT/ROLLBACK을 직접 실행)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")


def connect(db_path: Path) -> sqlite3.Connection:
    """새 연결을 하나 연다. 스레드마다 별도 연결을 쓴다(thread-local 원칙)."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    configure_connection(conn)
    return conn


class Writer:
    """단일 writer 스레드. 모든 쓰기는 이 큐를 거쳐 직렬화된다.

    사용법: `future = writer.submit(lambda conn: conn.execute(...)); future.result()`
    `fn`은 writer 전용 연결(conn)을 인자로 받아 실행되며, 트랜잭션 제어(BEGIN/COMMIT)는
    `fn` 내부에서 직접 한다.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="emailtomcp-db-writer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._conn = connect(self._db_path)
        try:
            while True:
                item = self._queue.get()
                if item is _SENTINEL:
                    break
                fn, fut = item
                if fut.cancelled():
                    continue
                try:
                    result = fn(self._conn)
                except BaseException as exc:  # noqa: BLE001 — 호출자에게 그대로 전달한다
                    if not fut.cancelled():
                        fut.set_exception(exc)
                else:
                    if not fut.cancelled():
                        fut.set_result(result)
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def submit(self, fn: Callable[[sqlite3.Connection], T]) -> Future[T]:
        """writer 스레드에서 `fn(conn)`을 실행한다. 결과는 반환된 Future로 받는다."""
        if self._thread is None:
            raise RuntimeError("Writer가 시작되지 않았습니다. start()를 먼저 호출하세요")
        fut: Future[T] = Future()
        self._queue.put((fn, fut))
        return fut

    def stop(self, timeout: float | None = 5.0) -> None:
        if self._thread is None:
            return
        self._queue.put(_SENTINEL)
        self._thread.join(timeout=timeout)
        self._thread = None


class Database:
    """DB 접근의 단일 창구. writer(쓰기)와 읽기 전용 연결 발급을 함께 관리한다."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.writer = Writer(db_path)

    def start(self) -> None:
        self.writer.start()

    def stop(self) -> None:
        self.writer.stop()

    def new_read_connection(self) -> sqlite3.Connection:
        """읽기 전용 용도의 새 연결을 연다. 호출자가 다 쓰면 close()해야 한다."""
        return connect(self.path)

    def migrate(self) -> int:
        """마이그레이션을 writer 스레드에서 실행하고, 적용된 user_version을 반환한다."""
        from emailtomcp.storage.migrations.runner import run_migrations

        def _do(conn: sqlite3.Connection) -> int:
            return run_migrations(conn)

        return self.writer.submit(_do).result()
