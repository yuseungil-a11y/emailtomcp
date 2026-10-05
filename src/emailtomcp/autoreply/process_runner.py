"""`core.ports.ProcessRunner` 운영 구현 — subprocess + psutil (DESIGN.md §2(c) "실행 관리").

- `shell=False`, stdin/stdout/stderr는 PIPE. Windows는 `CREATE_NO_WINDOW |
  CREATE_NEW_PROCESS_GROUP`, POSIX는 `start_new_session=True`.
- stdin은 별도 스레드에서 쓰고 닫는다(EOF, C-2). 큰 입력에서도 교착하지 않는다.
- stdout은 리더 스레드가 상한(기본 5MB)까지만 모은다. 넘으면 프로세스 트리를 종료한다.
- stderr는 마지막 4KB만 남긴다(진단용, 저장하지 않음).
- `kill_tree`: psutil로 자식 전체를 먼저, 그다음 부모를 종료한다.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

DEFAULT_MAX_STDOUT = 5 * 1024 * 1024
_STDERR_KEEP = 4096


@dataclass
class _Proc:
    popen: subprocess.Popen[bytes]
    stdout: bytearray = field(default_factory=bytearray)
    stderr_tail: bytearray = field(default_factory=bytearray)
    overflow: bool = False
    threads: list[threading.Thread] = field(default_factory=list)


class SubprocessRunner:
    def __init__(self, *, max_stdout_bytes: int = DEFAULT_MAX_STDOUT) -> None:
        self._max_stdout = max_stdout_bytes
        self._lock = threading.Lock()
        self._procs: dict[int, _Proc] = {}

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stdin_data: bytes | None = None,
    ) -> int:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        popen = subprocess.Popen(  # noqa: S603 — argv는 상수·앱 생성 경로뿐(C-1), shell=False
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
            start_new_session=sys.platform != "win32",
        )
        proc = _Proc(popen=popen)
        handle = popen.pid

        def _feed() -> None:
            try:
                if stdin_data and popen.stdin is not None:
                    popen.stdin.write(stdin_data)
            except OSError:
                pass
            finally:
                try:
                    if popen.stdin is not None:
                        popen.stdin.close()
                except OSError:
                    pass

        def _read_stdout() -> None:
            stream = popen.stdout
            if stream is None:
                return
            for chunk in iter(lambda: stream.read(65536), b""):
                if len(proc.stdout) + len(chunk) > self._max_stdout:
                    proc.overflow = True
                    self.kill_tree(handle)
                    break
                proc.stdout.extend(chunk)

        def _read_stderr() -> None:
            stream = popen.stderr
            if stream is None:
                return
            for chunk in iter(lambda: stream.read(4096), b""):
                proc.stderr_tail.extend(chunk)
                del proc.stderr_tail[:-_STDERR_KEEP]

        for target in (_feed, _read_stdout, _read_stderr):
            thread = threading.Thread(target=target, name=f"claude-{handle}-io", daemon=True)
            thread.start()
            proc.threads.append(thread)
        with self._lock:
            self._procs[handle] = proc
        return handle

    def _get(self, handle: int) -> _Proc:
        with self._lock:
            proc = self._procs.get(handle)
        if proc is None:
            raise KeyError(f"알 수 없는 프로세스 핸들: {handle}")
        return proc

    def wait(self, handle: int, *, timeout: float) -> int:
        proc = self._get(handle)
        try:
            return proc.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"claude 실행 시간 초과({timeout}초)") from exc

    def kill_tree(self, handle: int) -> None:
        try:
            parent = psutil.Process(handle)
        except psutil.Error:
            return
        try:
            children = parent.children(recursive=True)
        except psutil.Error:
            children = []
        for proc in (*children, parent):
            with contextlib.suppress(psutil.Error):
                proc.kill()
        psutil.wait_procs([*children, parent], timeout=5)

    def stdout_bytes(self, handle: int) -> bytes:
        proc = self._get(handle)
        for thread in proc.threads:
            thread.join(timeout=5)
        return bytes(proc.stdout)

    def overflowed(self, handle: int) -> bool:
        return self._get(handle).overflow

    def release(self, handle: int) -> None:
        with self._lock:
            proc = self._procs.pop(handle, None)
        if proc is None:
            return
        for stream in (proc.popen.stdout, proc.popen.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
