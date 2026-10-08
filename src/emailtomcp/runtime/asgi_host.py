"""Backend 루프 위에서 ASGI 앱(MCP)을 띄우는 범용 호스트 (DESIGN.md §2(a), §6.1 D-06, M2·L1).

- **소켓은 앱이 직접 만들어 uvicorn에 넘긴다.** Windows는 `SO_EXCLUSIVEADDRUSE`를 켜서
  다른 프로세스(같은 사용자의 다른 프로세스 포함)가 같은 포트에 끼어들어 바인딩하지
  못하게 한다. `SO_REUSEADDR`는 어떤 OS에서도 쓰지 않는다(M2).
- 바인딩 주소는 `127.0.0.1`만 쓴다(`::1`·`0.0.0.0` 금지, L1).
- 포트를 쓸 수 없으면 **다른 포트로 바꾸지 않고** `PortUnavailableError`를 낸다(C-05).
  호출자(app.py)가 MCP를 끈 상태로 두고 경고를 표시한다.
- SDK 앱을 `asgi_guard`로 감싸면 SDK 내장 lifespan이 돌지 않으므로, 이 호스트가 넘겨받은
  `lifespan()`(= `session_manager.run()`)을 uvicorn 실행 구간 바깥에서 직접 열고 닫는다(D-06).
  uvicorn 자체의 lifespan은 끈다(`lifespan="off"`).

이 모듈은 MCP 구현(mcp_server)을 알지 못한다 — ASGI 앱과 lifespan 컨텍스트만 받는다
(§4.2: runtime은 서비스 로직을 모른다).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import uvicorn

logger = logging.getLogger(__name__)

LOOPBACK_ADDRESS = "127.0.0.1"
GRACEFUL_SHUTDOWN_SECONDS = 3
DEFAULT_START_TIMEOUT_SECONDS = 10.0


class PortUnavailableError(RuntimeError):
    """포트를 이미 다른 프로세스가 쓰고 있거나 바인딩할 수 없다."""

    def __init__(self, port: int, reason: str) -> None:
        super().__init__(f"127.0.0.1:{port}에 바인딩할 수 없습니다: {reason}")
        self.port = port
        self.reason = reason


class _NotifyingServer(uvicorn.Server):
    """`uvicorn.Server`를 상속해 리슨 소켓을 닫기 **전에** 종료 콜백을 부른다(보안검토 S-1).

    `uvicorn.Server.shutdown()`은 시작 즉시 `server.close()`/`sock.close()`로 리슨
    소켓을 닫고, 그 뒤에야(연결 정리 대기 등을 거쳐) `serve()`가 반환한다. 예전 코드는
    `serve()`가 반환한 뒤(=소켓이 이미 닫힌 뒤) `on_exit`를 불러, "소켓 닫기 전"이라는
    주석과 실제 동작이 달랐다. `shutdown()`을 오버라이드해 첫 줄에서 알려 준 뒤 원래
    동작을 그대로 수행하면, 콜백 호출 시점에 소켓이 아직 열려 있다고 보장할 수 있다.
    """

    def __init__(self, config: uvicorn.Config, notify: Callable[[], None]) -> None:
        super().__init__(config)
        self._notify_before_shutdown = notify

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._notify_before_shutdown()
        await super().shutdown(sockets=sockets)


def create_loopback_socket(port: int) -> socket.socket:
    """`127.0.0.1:<port>` 전용 리슨 소켓을 만든다. 실패하면 `PortUnavailableError`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        sock.bind((LOOPBACK_ADDRESS, port))
        sock.listen(128)
        sock.setblocking(False)
    except OSError as exc:
        sock.close()
        raise PortUnavailableError(port, str(exc)) from exc
    return sock


class AsgiHost:
    """ASGI 앱 하나를 미리 만든 소켓으로 서비스한다. 모든 메서드는 Backend 루프에서 호출한다."""

    def __init__(
        self,
        app: Any,
        sock: socket.socket,
        *,
        lifespan: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        """`on_exit`는 서비스가 **어떤 이유로든** 끝났을 때(정상 종료·uvicorn 비정상 종료·
        시작 실패) Backend 루프에서 한 번 호출된다. 호출자는 이것으로 "실행 중" 상태를 즉시
        내린다(보안검토 M-3: 죽은 포트를 핸드셰이크가 계속 알려 주지 않게)."""
        self._app = app
        self._sock = sock
        # 종료 후에는 소켓이 닫혀 getsockname()을 쓸 수 없으므로 미리 기억해 둔다.
        self._port = int(sock.getsockname()[1])
        self._lifespan = lifespan
        self._on_exit = on_exit
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._started = asyncio.Event()

    @property
    def port(self) -> int:
        return self._port

    async def _serve(self) -> None:
        config = uvicorn.Config(
            self._app,
            lifespan="off",
            log_level="warning",
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            ws="none",
            http="h11",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
        server = _NotifyingServer(config, self._notify_exit)
        self._server = server
        lifespan_cm = self._lifespan() if self._lifespan is not None else contextlib.nullcontext()
        try:
            async with lifespan_cm:
                self._started.set()
                try:
                    await server.serve(sockets=[self._sock])
                finally:
                    # _NotifyingServer.shutdown()이 소켓을 닫기 전에 이미 _notify_exit()를
                    # 불렀다(S-1). 여기서는 그 다음에 호출돼 보통 아무 일도 하지 않지만,
                    # shutdown()이 전혀 불리지 않고 serve()가 끝나는 경로(예: 시작 직후
                    # 비정상 종료)에 대한 안전망으로 남겨 둔다 — lifespan 정리가 끝날
                    # 때까지 핸드셰이크가 닫히는 포트를 계속 알려 주는 창을 없앤다(N-3).
                    self._notify_exit()
                    self._close_socket()
        finally:
            # lifespan 진입 자체가 실패한 경우 등 — 위에서 이미 했으면 아무 일도 하지 않는다.
            self._started.set()
            self._notify_exit()
            self._close_socket()

    def _close_socket(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()

    def _notify_exit(self) -> None:
        """`on_exit`를 정확히 한 번 호출한다."""
        callback, self._on_exit = self._on_exit, None
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 — 종료 콜백 오류가 종료 자체를 막으면 안 된다
            logger.warning("ASGI 호스트 종료 콜백 오류", exc_info=True)

    async def start(self, *, timeout: float = DEFAULT_START_TIMEOUT_SECONDS) -> None:
        """서비스를 시작하고, uvicorn이 리슨을 시작할 때까지 기다린다.

        `timeout`초 안에 `self._started`가 set되지 않으면(예: lifespan 진입이 OS/네트워크
        스택 쪽 경합 등 바깥 요인으로 멈춤) 태스크를 취소하고 `TimeoutError`를 낸다 —
        그대로 두면 이미 bind·listen된 소켓을 영원히 쥔 채 아무도 멈추거나 재시도할 수
        없는 상태로 남는다(실제 장애 사례: EMCP-2026-1008). 취소돼도 `_serve()`의
        try/finally가 소켓을 닫으므로, 호출자는 실패 후 같은 포트로 다시 시작을 시도할 수
        있다.
        """
        self._task = asyncio.get_running_loop().create_task(
            self._serve(), name="emailtomcp-mcp-http"
        )
        try:
            await asyncio.wait_for(self._started.wait(), timeout=timeout)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            raise TimeoutError(
                f"MCP HTTP 서버가 {timeout:g}초 안에 시작되지 않았습니다"
            ) from None
        for _ in range(100):
            if self._task.done() or (self._server is not None and self._server.started):
                break
            await asyncio.sleep(0.02)
        if self._task.done():
            # 시작 중 실패 — 예외를 그대로 올린다.
            await self._task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self, timeout: float = 10.0) -> None:
        """uvicorn과 세션 매니저를 순서대로 종료한다(§2(a) 종료 순서 4번)."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except TimeoutError:
            logger.warning("MCP HTTP 서버가 %s초 안에 종료되지 않아 취소합니다", timeout)
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        except Exception:  # noqa: BLE001 — 종료 중 오류는 로그만 남긴다
            logger.warning("MCP HTTP 서버 종료 중 오류", exc_info=True)
