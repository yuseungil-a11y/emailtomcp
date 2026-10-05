"""DI(의존성 주입) 포트 (DESIGN.md §4.6, 갈릴레오 0장).

여기 정의된 Protocol 각각은 "운영 구현"과 "테스트 구현"을 갈아끼울 수 있게 만드는
경계다. `Clock`은 `core/clock.py`에 이미 있으므로(테스트가 참조 중) 그대로 두고,
나머지 5개의 인터페이스만 이 모듈에 둔다.

구현체는 각 패키지가 P1~P3에서 채운다(지금은 인터페이스만 있고 구현은 없다):
- ProcessRunner: `autoreply/`가 `claude -p`를 실행할 때 쓴다(subprocess+psutil).
- ShellProbe: `autoreply/cli_locator.py`가 로그인 셸을 탐지할 때 쓴다(사용자 요청 시에만).
- SecretStore: `secrets/keyring_store.py`가 구현한다.
- HttpClient: `update/release_client.py`가 매니페스트·자산을 받을 때 쓴다(httpx+truststore).
- JobTokenIssuer: `mcp_server/auth.py`가 구현하고 `autoreply/`에 주입된다(순환 의존 해소,
  DESIGN.md §4.2 S-07).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProcessRunner(Protocol):
    """자식 프로세스 실행 포트(spawn/wait/kill_tree).

    운영 구현은 `subprocess`(shell=False) + `psutil`(프로세스 트리 종료)이다.
    테스트 구현은 `FakeRunner`(단위 테스트) 또는 실제 `fake_claude.py`(통합 테스트, 실행
    파일을 바꿔치기)를 쓴다.
    """

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stdin_data: bytes | None = None,
    ) -> int:
        """자식 프로세스를 실행하고 핸들/식별자(구현체가 정의)를 반환한다."""
        ...

    def wait(self, handle: int, *, timeout: float) -> int:
        """종료 코드를 기다린다. 타임아웃이면 `TimeoutError`를 낸다."""
        ...

    def kill_tree(self, handle: int) -> None:
        """프로세스와 그 자식 전체를 종료한다(psutil 기반)."""
        ...

    def stdout_bytes(self, handle: int) -> bytes:
        """(P3 Phase B 추가) 종료 후 수집한 stdout(상한까지). stream-json 사후 검증 근거."""
        ...

    def release(self, handle: int) -> None:
        """(P3 Phase B 추가) 핸들과 파이프·리더 스레드를 정리한다(여러 번 불러도 안전)."""
        ...


@runtime_checkable
class ShellProbe(Protocol):
    """로그인 셸 탐지 포트. 사용자가 명시적으로 요청했을 때만 호출한다(DESIGN.md §9.3)."""

    def detect_login_shell(self) -> str | None:
        """로그인 셸 경로를 반환한다. 알 수 없으면 None."""
        ...


@runtime_checkable
class SecretStore(Protocol):
    """비밀번호/토큰 저장 포트. 운영 구현은 OS keyring, 테스트는 인메모리(autouse fixture)."""

    def get(self, service: str, key: str) -> str | None: ...

    def set(self, service: str, key: str, value: str) -> None: ...

    def delete(self, service: str, key: str) -> None: ...


@runtime_checkable
class HttpClient(Protocol):
    """HTTP 클라이언트 포트. `base_url`을 주입받아 테스트에서 fake 서버를 가리킬 수 있게 한다
    (갈릴레오 G-03, G-05). 운영 구현은 httpx(truststore), 테스트는 fake_github_releases.
    """

    base_url: str

    def get(self, path: str, *, timeout: float = 10.0) -> Any: ...


@runtime_checkable
class JobTokenIssuer(Protocol):
    """잡 토큰 발급/검증 포트.

    `autoreply/`는 이 Protocol만 알고, 실제 구현(`mcp_server.auth`)은 app.py가
    주입한다 — `mcp_server`와 `autoreply` 사이의 순환 의존을 끊기 위한 경계다
    (DESIGN.md §4.2 S-07).
    """

    def issue(self, job_id: int) -> str:
        """`token_urlsafe(32)` 형태의 잡 토큰을 발급한다. 메모리에만 보관한다."""
        ...

    def revoke(self, job_id: int) -> None:
        """잡 종료/타임아웃/강제종료 시 토큰을 즉시 폐기한다."""
        ...


@runtime_checkable
class JobSubmissionSink(Protocol):
    """잡 제출 본문을 메모리로만 넘기는 포트(§7.9 G5 "본문은 메모리(pending_verification)에만").

    `mcp_server`(submit_auto_reply)가 G5 조건부 UPDATE에 성공한 뒤 이 포트로 본문을 넘기고,
    구현(`autoreply.job_runner.JobRunner`)은 app.py가 주입한다. 본문을 이벤트 버스에 싣지 않는
    이유는 버스가 UI(qt_bridge 와일드카드 구독)까지 퍼지기 때문이다. DB에는 해시만 남는다.
    """

    def accept_submission(
        self,
        job_id: int,
        *,
        decision: str,
        body_text: str,
        reason: str | None,
        body_sha256: str,
    ) -> None: ...


@runtime_checkable
class AutoSendVerifier(Protocol):
    """G7 권위 판정 포트(§7.11 M-D).

    **유일한 프로덕션 구현**은 `rules/autosend_verify.py`이고, app.py가
    `AutoReplyRepository(db, verifier=...)` 생성자에 한 번 주입한다(아키텍처 테스트로 강제).
    repo 함수는 호출 인자로 판정 함수를 받지 않는다(§7.10 I-1).

    `conn`은 G7 writer 트랜잭션의 연결이다. 반환값은 `core.autoreply_types.AutoSendVerdict`.
    """

    def verify(
        self,
        conn: Any,
        job: Any,
        *,
        guard: Any,
        preflight: Any,
    ) -> Any: ...
