"""자동회신 전역 토글 런타임 컨트롤러 (DESIGN.md §7.0, §7.11) — P3 Phase A.

상태: OFF → STARTING → ON → STOPPING → OFF. `disable()`/`enable()`은 `asyncio.Lock` 하나로
직렬화한다(토글을 동시에 두 번 처리하지 않음). Backend asyncio 루프에서 동작하고, DB 작업은
`storage.repositories.autoreply`(writer 스레드)에 맡긴다.

- 메모리의 `_active`·`_generation`은 **빠른 길 판단에만** 쓴다. 차단은 언제나 writer 관문
  (§7.9 G3·G4·G7·G8, Phase B 이후)이 DB 값으로 한다 — 메모리 상태가 늦게 바뀌어도 발송으로
  이어지지 않는다.
- `disable()`은 **CAS 없이 항상 성공**한다(보안리뷰 L-A). `enable()`은 CAS다.
- 끄기 순서는 "DB를 먼저 닫고, 그다음 런타임을 멈춘다"(§7.0): 1단계 writer 트랜잭션을 커밋한
  뒤에야 메모리 상태를 내리고 이벤트를 낸다.

**Phase B 연결**: 생성자로 받은 `engine`(rules.engine.RuleEngine)과 `runner`
(autoreply.job_runner.JobRunner)를 켜기·끄기 순서에 맞춰 붙인다. 둘 다 None이면 Phase A와 같은
"토글만 관리하는" 동작이다(기존 테스트 호환). §4.2상 autoreply는 rules를 import하지 않으므로
엔진은 덕 타이핑(`start/stop/catch_up`)으로만 다룬다.

- 켜기(커밋 뒤): 러너 시작 → 엔진 구독(MessageReceived) → 1회 따라잡기 → 러너 깨움 → 이벤트
- 끄기(커밋 뒤): 엔진 구독 해제 → 실행 중이던 잡의 **토큰 폐기 → 프로세스 트리 종료**
  (`runner.abort_jobs`, §7.10 S-1 순서) → 러너 정지 → 이벤트
- 기동(on): 복구 트랜잭션 뒤 켜기와 같은 순서. 기동(off): 엔진·러너를 시작하지 않는다(I2).
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from emailtomcp.core.errors import PolicyError
from emailtomcp.storage.repositories import autoreply as autoreply_repo

if TYPE_CHECKING:
    from emailtomcp.core.clock import Clock
    from emailtomcp.core.events import EventBus
    from emailtomcp.storage.db import Database
    from emailtomcp.storage.repositories.autoreply import DrainReport, RecoveryReport

logger = logging.getLogger(__name__)

# 토글이 바뀌면 발행하는 이벤트(§11.1 상태줄 갱신 근거).
# payload: {"enabled": bool, "generation": int | None}
EVENT_AUTOREPLY_ENABLED_CHANGED = "AutoReplyEnabledChanged"


class ControllerState(enum.StrEnum):
    OFF = "off"
    STARTING = "starting"
    ON = "on"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class ToggleResult:
    """`disable()`/`enable()` 결과. 끄기면 `drain`에 1단계 결과가 들어 있다."""

    enabled: bool
    generation: int | None
    drain: DrainReport | None = None


class AutoReplyController:
    def __init__(
        self,
        db: Database,
        *,
        clock: Clock,
        event_bus: EventBus | None = None,
        engine: Any = None,
        runner: Any = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._event_bus = event_bus
        self._engine = engine
        self._runner = runner
        self._lock = asyncio.Lock()
        self._state = ControllerState.OFF
        self._active = False
        self._generation: int | None = None

    # ------------------------------------------------------------ 조회(빠른 길 전용)

    @property
    def state(self) -> ControllerState:
        return self._state

    @property
    def active(self) -> bool:
        """메모리상 켜짐 여부. **판정 근거로 쓰지 않는다**(writer 관문이 DB 값으로 판정)."""
        return self._active

    @property
    def generation(self) -> int | None:
        return self._generation

    # ------------------------------------------------------------ 내부

    @staticmethod
    async def _in_executor(fn, /, **kwargs):  # noqa: ANN001, ANN003, ANN205 — 내부 전용
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(**kwargs))

    def _publish(self) -> None:
        if self._event_bus is None:
            return
        self._event_bus.publish(
            EVENT_AUTOREPLY_ENABLED_CHANGED,
            {"enabled": self._active, "generation": self._generation},
        )

    def _set_off(self, generation: int | None) -> None:
        self._active = False
        self._generation = generation
        self._state = ControllerState.OFF

    async def _start_runtime(self, generation: int) -> None:
        """켜진 뒤(DB 커밋 뒤) 런타임 시작: 러너 → 엔진 구독 → 1회 따라잡기 → 러너 깨움."""
        runner, engine = self._runner, self._engine
        if runner is not None:
            await self._in_executor(runner.start)
        if engine is not None:
            engine.start(generation)
            try:
                count = await self._in_executor(engine.catch_up, expected_generation=generation)
                logger.info("자동회신 따라잡기 평가: %s건", count)
            except Exception:  # noqa: BLE001 — 따라잡기 실패가 켜기를 되돌리지는 않는다
                logger.exception("자동회신 따라잡기 실패")
        if runner is not None:
            runner.wake()

    async def _stop_runtime(self, running_job_ids: tuple[int, ...] = ()) -> None:
        """끈 뒤(DB 커밋 뒤) 런타임 정지: 구독 해제 → 토큰 폐기 → kill → 러너 정지(S-1)."""
        if self._engine is not None:
            self._engine.stop()
        runner = self._runner
        if runner is not None:
            if running_job_ids:
                runner.abort_jobs(list(running_job_ids))
            await self._in_executor(runner.stop)

    # ------------------------------------------------------------ 기동·종료

    async def start_if_enabled(self) -> RecoveryReport:
        """앱 기동 시 1회. 복구 트랜잭션을 실행하고 그 결과(유효값)에 따라 상태를 정한다.

        off면 규칙 로드·RE2·CLI 탐지를 하지 않는다(Phase B 이후에도 이 분기를 유지한다).
        """
        async with self._lock:
            read_state = await self._in_executor(self._read_state)
            report: RecoveryReport = await self._in_executor(
                autoreply_repo.recover_on_startup, db=self._db, enabled=read_state.enabled
            )
            if report.enabled and report.generation is not None and report.generation >= 1:
                self._active = True
                self._generation = report.generation
                self._state = ControllerState.ON
                await self._start_runtime(report.generation)
            else:
                self._set_off(report.generation)
            logger.info(
                "자동회신 기동 복구: enabled=%s generation=%s cancelled=%s reverted=%s requeued=%s",
                report.enabled,
                report.generation,
                report.cancelled_jobs,
                report.reverted_outbox,
                report.requeued_jobs,
            )
            return report

    def _read_state(self) -> autoreply_repo.ToggleState:
        conn = self._db.new_read_connection()
        try:
            return autoreply_repo.get_toggle_state(conn)
        finally:
            conn.close()

    async def shutdown(self) -> None:
        """앱 종료 시 정리. 저장된 토글 값은 바꾸지 않는다(다음 기동에 그대로 이어진다).

        엔진 구독을 해제하고 러너를 멈춘다(실행 중 잡은 토큰 폐기 후 프로세스 종료 — DB 상태는
        다음 기동 복구가 정리한다).
        """
        async with self._lock:
            self._active = False
            self._state = ControllerState.OFF
            await self._stop_runtime()

    # ------------------------------------------------------------ 토글

    async def disable(self) -> ToggleResult:
        """끄기 — **CAS 없음, 항상 성공**(L-A). 세대가 달라도 거부하지 않는다.

        DB 1단계(`disable_and_drain`)가 예외로 끝나면(DB 오류) 메모리 상태는 그래도 off로 내리고
        예외를 그대로 올린다 — 메모리 쪽은 언제나 안전한 방향으로 둔다.
        """
        async with self._lock:
            self._state = ControllerState.STOPPING
            self._active = False  # 빠른 길은 커밋 전에 먼저 닫는다(안전한 방향)
            try:
                report: DrainReport = await self._in_executor(
                    autoreply_repo.disable_and_drain, db=self._db, now=self._clock.now()
                )
            except BaseException:
                self._state = ControllerState.OFF
                raise
            self._set_off(report.generation)
            # 끄기 2단계: 구독 해제 → 잡 토큰 폐기 → 프로세스 트리 종료(§7.10 S-1, 순서 고정).
            try:
                await self._stop_runtime(report.cancelled_running_job_ids)
            except Exception:  # noqa: BLE001 — 1단계(DB)가 이미 막았으므로 fail-closed
                logger.exception("자동회신 끄기 2단계 실패(이후 제출·확정은 DB 관문이 거부)")
            self._publish()
            return ToggleResult(enabled=False, generation=report.generation, drain=report)

    async def enable(self, *, expected_generation: int) -> ToggleResult:
        """켜기 — compare-and-set. 세대가 다르면 `PolicyError`를 그대로 올린다(R-h)."""
        if not isinstance(expected_generation, int) or isinstance(expected_generation, bool):
            raise PolicyError("expected_generation은 정수여야 합니다")
        async with self._lock:
            previous = (self._state, self._active, self._generation)
            self._state = ControllerState.STARTING
            try:
                new_generation: int = await self._in_executor(
                    autoreply_repo.enable,
                    db=self._db,
                    expected_generation=expected_generation,
                    now=self._clock.now(),
                )
            except BaseException:
                self._state, self._active, self._generation = previous
                raise
            self._active = True
            self._generation = new_generation
            self._state = ControllerState.ON
            # 켜기 3단계: 러너 시작 → MessageReceived 구독 → 1회 따라잡기(RE2 컴파일은 평가 시).
            await self._start_runtime(new_generation)
            self._publish()
            return ToggleResult(enabled=True, generation=new_generation)
