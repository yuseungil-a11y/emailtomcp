from __future__ import annotations

from emailtomcp.runtime.backend import Backend


def test_backend_submit_runs_coroutine_and_returns_result() -> None:
    backend = Backend()
    backend.start()
    try:

        async def _answer() -> int:
            return 42

        result = backend.submit(_answer(), timeout=2)
        assert result == 42
    finally:
        backend.stop()


def test_backend_stop_leaves_no_thread_or_loop() -> None:
    backend = Backend()
    backend.start()
    backend.stop()

    assert backend._thread is None
    assert backend._loop is None


def test_backend_rejects_new_jobs_once_stop_begins() -> None:
    backend = Backend()
    backend.start()
    backend._accepting_jobs = False  # stop() 호출 직후 상태를 흉내낸다

    async def _answer() -> int:
        return 1

    raised = False
    try:
        backend.submit(_answer(), timeout=1)
    except RuntimeError:
        raised = True
    finally:
        backend._accepting_jobs = True
        backend.stop()

    assert raised


def test_backend_propagates_exceptions_from_coroutine() -> None:
    backend = Backend()
    backend.start()
    try:

        async def _boom() -> None:
            raise ValueError("의도된 실패")

        raised = False
        try:
            backend.submit(_boom(), timeout=2)
        except ValueError:
            raised = True
        assert raised
    finally:
        backend.stop()
