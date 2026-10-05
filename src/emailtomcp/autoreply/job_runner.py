"""자동회신 잡 러너 — G4 → (실행) → G5 → G6 → G7 (DESIGN.md §2(c), §7.9) — P3 Phase B.

전용 스레드 하나(동시성 1, §2(c) 기본값)가 잡을 하나씩 처리한다. 폴링하지 않고 `wake()`(G3 직후
RuleEngine이 부름)와 기동 시 1회 깨움으로만 일한다. 처리 순서:

1. **G4** `claim_next_job`(writer: gate·세대·queue_state 확인, queued→running).
   토큰은 커밋 뒤에만 발급한다.
2. 고정 템플릿 규칙이면 claude 없이 본문을 만들고 `mark_submitted`(G5와 같은 조건부 UPDATE).
3. Claude 생성이면:
   - claude CLI 고정·해시 확인(§9.3). 실패 → failed(permanent)
   - 잡 디렉터리 생성(DACL) → **사전점검**. 설정 격리 `fallback3`이면 claude를 실행하지 않고
     잡을 cancelled(`isolation_fallback3`)로 끝낸다(Phase B 보수 정책, preflight.py 참고)
   - 잡 토큰 발급 → mcp-config(토큰은 `${VAR}` 참조) → argv/env allowlist/stdin으로 실행
   - 제출은 MCP `submit_auto_reply`가 G5로 DB에 해시를 남기고 이 러너의 `accept_submission`으로
     본문을 메모리에 넘긴다(본문은 DB·이벤트 버스에 싣지 않는다)
   - 종료(또는 타임아웃 kill) → **토큰 폐기** → G6 사후 검증(종료 코드, 도구 ⊆ 허용, submit 정확히
     1회, turns 상한, 메모리 본문 해시 = DB submission_sha256) → 사후 점검(PreflightResult)
4. 출력 가드(주입된 `guard_check`, §4.2상 autoreply는 rules를 import하지 않는다)
5. **G7** `AutoReplyRepository.commit_autoreply_result` — Phase B는 언제나 초안(draft)만.
6. 잡 디렉터리 삭제. 실패하면 autosend_state=paused_security(§7.11 fail-closed).

끄기 2단계(§7.0): 컨트롤러가 `abort_jobs(ids)`를 부르면 **토큰을 먼저 폐기하고** 그다음 프로세스
트리를 종료한다(§7.10 S-1 순서).

미룬 것: TransientError 재시도(30초·120초, §2(c)) — 이번 단계는 타임아웃·MCP 미가동을 failed
(transient)로 끝낸다. AuthError 시 queue_state=paused_auth도 다음 단계.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from emailtomcp.autoreply import job_dir as job_dir_mod
from emailtomcp.autoreply import preflight as preflight_mod
from emailtomcp.autoreply.claude_runner import (
    DEFAULT_TIMEOUT_SEC,
    MAX_TIMEOUT_SEC,
    MAX_TURNS,
    MIN_TIMEOUT_SEC,
    build_argv,
    parse_stream,
)
from emailtomcp.autoreply.cli_locator import SETTING_CLI_PIN, pin_from_setting, verify_pin
from emailtomcp.autoreply.env_policy import (
    ALLOWED_TOOLS_AUTO_REPLY_DRAFT,
    JOB_TOKEN_ENV,
    build_child_env,
)
from emailtomcp.autoreply.prompts import (
    UNMATCHED_RULE_NAME,
    build_user_prompt,
    make_reply_subject,
    render_fixed_template,
)
from emailtomcp.config.settings import get_setting
from emailtomcp.core.autoreply_types import GuardResult, PreflightResult, ProposedReply
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import autoreply as autoreply_repo
from emailtomcp.storage.repositories import messages as messages_repo
from emailtomcp.storage.repositories import rules as rules_repo

if TYPE_CHECKING:
    from emailtomcp.core.autoreply_types import AutoReplyJobRow
    from emailtomcp.core.clock import Clock
    from emailtomcp.core.ports import JobTokenIssuer, ProcessRunner
    from emailtomcp.storage.db import Database
    from emailtomcp.storage.repositories.autoreply import AutoReplyRepository, CommitOutcome

logger = logging.getLogger(__name__)

GuardCheck = Callable[..., GuardResult]
EVENT_JOB_FINISHED = "JobFinished"


@dataclass(frozen=True, slots=True)
class Submission:
    decision: str
    body_text: str
    reason: str | None
    body_sha256: str


def _domain(addr_norm: str) -> str:
    return addr_norm.rpartition("@")[2] if "@" in addr_norm else ""


def _local_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().strftime("%Y-%m-%d")


class JobRunner:
    def __init__(
        self,
        db: Database,
        *,
        clock: Clock,
        repo: AutoReplyRepository,
        token_issuer: JobTokenIssuer,
        process_runner: ProcessRunner,
        jobs_base_dir: Path,
        mcp_port: Callable[[], int | None],
        guard_check: GuardCheck,
        event_bus: Any = None,
        claude_config_dir: Path | None = None,
        home_dir: Path | None = None,
        env_source: Mapping[str, str] | None = None,
        managed_settings_dirs: Sequence[Path] | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._repo = repo
        self._tokens = token_issuer
        self._proc = process_runner
        self._jobs_base = jobs_base_dir
        self._mcp_port = mcp_port
        self._guard = guard_check
        self._event_bus = event_bus
        self._config_dir = claude_config_dir
        self._home = home_dir
        self._env_source = env_source
        self._managed_dirs = managed_settings_dirs
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_job: int | None = None
        self._current_handle: int | None = None
        self._submissions: dict[int, Submission] = {}
        self._aborted: set[int] = set()

    # ------------------------------------------------------------ 수명

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autoreply-runner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            current = self._current_job
        if current is not None:
            self.abort_jobs([current])
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:  # noqa: BLE001 — 러너 스레드는 죽지 않는다
                logger.exception("자동회신 잡 처리 중 예외")
                worked = False
            if not worked:
                self._wake.wait(timeout=60.0)
                self._wake.clear()

    # ------------------------------------------------------------ 끄기 2단계·제출

    def abort_jobs(self, job_ids: list[int] | tuple[int, ...]) -> None:
        """토큰을 **먼저** 폐기한 뒤 실행 중 프로세스 트리를 종료한다(§7.0 끄기 2단계, S-1)."""
        ids = [int(j) for j in job_ids]
        for job_id in ids:
            try:
                self._tokens.revoke(job_id)
            except Exception:  # noqa: BLE001
                logger.warning("잡 토큰 폐기 실패: job_id=%s", job_id, exc_info=True)
        with self._lock:
            self._aborted.update(ids)
            handle = self._current_handle if self._current_job in ids else None
        if handle is not None:
            try:
                self._proc.kill_tree(handle)
            except Exception:  # noqa: BLE001
                logger.warning("claude 프로세스 종료 실패: handle=%s", handle, exc_info=True)

    def revalidate_current(self) -> bool:
        """실행 중 잡의 gate를 다시 확인해 불통과면 DB 취소(커밋) → `abort_jobs`(토큰 폐기 → kill).

        규칙 삭제·변경·중지, 계정 비활성 저장이 **커밋된 뒤** UiApi가 부른다(데카르트 31번 L-4).
        예전에는 DB만 cancelled가 되고 claude 프로세스는 타임아웃까지 계속 돌았다(G7 CAS가 결과를
        막으므로 안전 문제는 없었지만 자원·토큰 노출 시간이 길었다). 취소했으면 True.
        """
        with self._lock:
            job_id = self._current_job
        if job_id is None:
            return False
        conn = self._db.new_read_connection()
        try:
            job = autoreply_repo.get_job(conn, job_id)
            gate = (
                autoreply_repo.check_autoreply_gate(
                    conn,
                    account_id=job.account_id,
                    rule_id=job.rule_id,
                    rule_version=job.rule_version,
                    job_generation=job.enable_gen,
                )
                if job is not None and job.status == "running"
                else None
            )
        finally:
            conn.close()
        if gate is not None and gate.ok:
            return False
        if gate is not None:
            # 아직 running이면(규칙 변경·중지, 계정 비활성) 먼저 DB에서 취소를 커밋한다.
            autoreply_repo.cancel_running_job(
                self._db,
                job_id=job_id,
                reason=gate.reason_code or "gate_failed",
                now=self._clock.now(),
            )
        self.abort_jobs([job_id])
        return True

    def accept_submission(
        self,
        job_id: int,
        *,
        decision: str,
        body_text: str,
        reason: str | None,
        body_sha256: str,
    ) -> None:
        """`core.ports.JobSubmissionSink` — MCP G5 성공 뒤 본문을 메모리로 받는다.

        지금 이 러너가 실행 중인 잡의 제출만 받는다(그 밖은 버린다 → G7에서 본문 없음으로 실패).
        """
        with self._lock:
            if self._current_job != job_id:
                logger.warning("실행 중이 아닌 잡의 제출을 버립니다: job_id=%s", job_id)
                return
            self._submissions[job_id] = Submission(
                decision=decision, body_text=body_text, reason=reason, body_sha256=body_sha256
            )

    # ------------------------------------------------------------ 처리

    def run_once(self) -> bool:
        """잡 하나를 가져와 끝까지 처리한다. 가져온 잡이 없으면 False."""
        if self._stop.is_set():
            return False
        # MCP 서버가 아직(또는 포트 충돌로) 없으면 잡을 가져가지 않는다 — queued로 두고 서버가
        # 켜질 때(McpStatusChanged) 다시 깨어난다. 가져간 뒤 실패로 끝내지 않기 위해서다.
        if self._mcp_port() is None:
            return False
        job = autoreply_repo.claim_next_job(self._db, now=self._clock.now())
        if job is None:
            return False
        with self._lock:
            self._current_job = job.id
            self._current_handle = None
            self._submissions.pop(job.id, None)
            self._aborted.discard(job.id)
        try:
            outcome = self._process(job)
            logger.info("자동회신 잡 처리: job_id=%s outcome=%s", job.id, outcome)
        except Exception:
            logger.exception("자동회신 잡 처리 실패: job_id=%s", job.id)
            self._fail(job, "permanent", "runner_error")
        finally:
            with self._lock:
                self._current_job = None
                self._current_handle = None
                self._submissions.pop(job.id, None)
            if self._event_bus is not None:
                self._event_bus.publish(EVENT_JOB_FINISHED, {"job_id": job.id})
        return True

    def _fail(self, job: AutoReplyJobRow, error_class: str, reason: str, **kw: Any) -> str:
        autoreply_repo.fail_running_job(
            self._db,
            job_id=job.id,
            error_class=error_class,
            reason=reason,
            now=self._clock.now(),
            **kw,
        )
        return f"failed:{reason}"

    def _timeout_sec(self, conn: Any) -> int:
        value = get_setting(conn, "autoreply.timeout_sec", None)
        if not isinstance(value, int) or isinstance(value, bool):
            value = get_setting(conn, "autoreply_timeout_sec", DEFAULT_TIMEOUT_SEC)
        if not isinstance(value, int) or isinstance(value, bool):
            value = DEFAULT_TIMEOUT_SEC
        return max(MIN_TIMEOUT_SEC, min(MAX_TIMEOUT_SEC, value))

    def _process(self, job: AutoReplyJobRow) -> str:
        conn = self._db.new_read_connection()
        try:
            facts = messages_repo.get_autoreply_facts(conn, job.message_id)
            account = accounts_repo.get_account(conn, job.account_id)
            rule = rules_repo.get_rule(conn, job.rule_id) if job.rule_id is not None else None
            pin = pin_from_setting(get_setting(conn, SETTING_CLI_PIN, None))
            timeout = self._timeout_sec(conn)
        finally:
            conn.close()
        if facts is None or account is None:
            return self._fail(job, "permanent", "message_missing")
        if job.rule_id is not None and rule is None:
            autoreply_repo.cancel_running_job(
                self._db, job_id=job.id, reason="rule_missing", now=self._clock.now()
            )
            return "cancelled:rule_missing"
        to_norm = facts.from_addr_norm or ""
        if not facts.from_addr or not to_norm:
            return self._fail(job, "permanent", "no_recipient")
        subject = make_reply_subject(facts.subject)
        max_chars = rule.max_chars if rule is not None else 400

        if rule is not None and rule.reply_mode == "fixed_template":
            body = render_fixed_template(
                rule.fixed_template or "",
                sender_name=facts.from_name,
                received_date=_local_date(facts.date_hdr or facts.received_at),
                account_name=account.display_name,
            )
            if not autoreply_repo.mark_submitted(
                self._db,
                job_id=job.id,
                body_sha256=autoreply_repo.sha256_text(body),
                now=self._clock.now(),
            ):
                return "aborted:not_running"
            preflight = preflight_mod.for_fixed_template(
                job_id=job.id, attempt=job.attempts, now=self._clock.now()
            )
            submission = Submission("reply", body, None, autoreply_repo.sha256_text(body))
            return self._commit(job, facts, submission, subject, max_chars, preflight)

        problem = verify_pin(pin)
        if problem is not None or pin is None:
            return self._fail(job, "permanent", "claude_cli_unavailable")
        port = self._mcp_port()
        if port is None:
            return self._fail(job, "transient", "mcp_unavailable")
        try:
            jdir = job_dir_mod.create_job_dir(self._jobs_base, job_id=job.id, attempt=job.attempts)
        except OSError:
            logger.exception("잡 디렉터리 생성 실패: job_id=%s", job.id)
            return self._fail(job, "permanent", "jobdir_failed")
        try:
            return self._run_claude(
                job, facts, account, rule, pin, port, jdir, subject, max_chars, timeout
            )
        finally:
            if not job_dir_mod.remove_job_dir(jdir):
                logger.error("잡 디렉터리를 지우지 못해 자동발송을 보안 정지합니다: %s", jdir.root)
                autoreply_repo.pause_autosend_for_security(self._db)

    def _run_claude(
        self,
        job: AutoReplyJobRow,
        facts: Any,
        account: Any,
        rule: Any,
        pin: Any,
        port: int,
        jdir: job_dir_mod.JobDir,
        subject: str,
        max_chars: int,
        timeout: int,
    ) -> str:
        # 사전점검 대상과 자식 env를 **같은 Mapping**에서 결정한다(Spinoza 30번 H-1). os.environ은
        # 한 번 스냅샷을 떠서 두 곳이 엄밀히 같은 값을 보게 한다(34번 Info-2).
        env_source = self._env_source if self._env_source is not None else dict(os.environ)
        pre = preflight_mod.run_pre_checks(
            job_id=job.id,
            attempt=job.attempts,
            job_dir=jdir,
            now=self._clock.now(),
            config_dir=self._config_dir,
            home=self._home,
            env_source=env_source,
            managed_dirs=self._managed_dirs,
        )
        if pre.isolation_mode == "fallback3":
            logger.warning(
                "설정 격리 폴백3 상태라 claude를 실행하지 않고 잡을 취소합니다: job_id=%s %s",
                job.id,
                ",".join(pre.isolation_findings),
            )
            autoreply_repo.cancel_running_job(
                self._db, job_id=job.id, reason="isolation_fallback3", now=self._clock.now()
            )
            return "cancelled:isolation_fallback3"

        token = self._tokens.issue(job.id)  # G4 커밋 뒤 발급(§7.9)
        handle: int | None = None
        try:
            config_path = job_dir_mod.write_mcp_config(jdir, port=port, token_env=JOB_TOKEN_ENV)
            argv = build_argv(
                pin, mcp_config=config_path, allowed_tools=ALLOWED_TOOLS_AUTO_REPLY_DRAFT
            )
            env = build_child_env(
                env_source,
                job_tmp=jdir.tmp,
                token=token,
                program_dirs=pin.program_dirs(),
            )
            prompt = build_user_prompt(
                account_name=account.display_name,
                rule_name=rule.name if rule is not None else UNMATCHED_RULE_NAME,
                extra_instructions=rule.extra_instructions if rule is not None else None,
                max_chars=max_chars,
            )
            with self._lock:
                if job.id in self._aborted:
                    return "aborted:before_spawn"
            handle = self._proc.spawn(
                argv, cwd=jdir.work, env=env, stdin_data=prompt.encode("utf-8")
            )
            with self._lock:
                self._current_handle = handle
                aborted = job.id in self._aborted
            if aborted:
                self._proc.kill_tree(handle)
            try:
                exit_code: int | None = self._proc.wait(handle, timeout=float(timeout))
            except TimeoutError:
                self._tokens.revoke(job.id)
                self._proc.kill_tree(handle)
                # 타임아웃이어도 허용 외 도구 시도를 먼저 판정한다(Spinoza 30번 L-1).
                timed_out = self._security_failure(
                    job, parse_stream(self._proc.stdout_bytes(handle))
                )
                return timed_out or self._fail(job, "transient", "timeout")
            stdout = self._proc.stdout_bytes(handle)
        finally:
            self._tokens.revoke(job.id)  # 제출·타임아웃·강제종료 어느 경우든 즉시 폐기
            if handle is not None:
                try:
                    self._proc.release(handle)
                except Exception:  # noqa: BLE001
                    logger.debug("프로세스 핸들 정리 실패", exc_info=True)

        with self._lock:
            submission = self._submissions.pop(job.id, None)
            aborted = job.id in self._aborted
        report = parse_stream(stdout)
        # G6 사후 검증 — 허용 외 도구·submit 2회 이상은 보안 폐기(+paused_security), 그 밖은
        # 폐기(permanent). stdout 상한 초과(overflow)는 kill 뒤 잘린 stdout을 여기서 같이 본다.
        # 보안 판정은 중지(aborted) 확인보다 **먼저** 한다(Spinoza 34번 N-3): 사용자가 잡을
        # 중지한 시점과 허용 외 도구 시도가 겹쳐도 보안 신호(paused_security)를 잃지 않는다.
        security = self._security_failure(job, report)
        if security is not None:
            return security
        if aborted:
            return "aborted"
        if exit_code != 0 or report.is_error or not report.result_seen:
            return self._fail(job, "permanent", "claude_exit", discard=True)
        if report.turns is not None and report.turns > MAX_TURNS:
            return self._fail(job, "permanent", "turns_exceeded", discard=True)
        if report.submit_count != 1 or submission is None:
            return self._fail(job, "permanent", "submit_count", discard=True)

        stored = self._stored_submission_hash(job.id)
        body_hash = autoreply_repo.sha256_text(submission.body_text)
        verified = body_hash if stored is not None and stored == body_hash else None
        preflight = preflight_mod.finalize(
            pre,
            job_dir=jdir,
            now=self._clock.now(),
            run=preflight_mod.RunFacts(
                tools_used=report.tools_used,
                submit_count=report.submit_count,
                turns=report.turns,
                exit_code=exit_code,
            ),
            verified_body_sha256=verified,
        )
        return self._commit(job, facts, submission, subject, max_chars, preflight)

    def _security_failure(self, job: AutoReplyJobRow, report: Any) -> str | None:
        """G6 보안 등급 판정: 허용 외 도구 시도, submit 2회 이상(인젝션 전형 신호, L-1).

        잡이 이미 running이 아니면(끄기·규칙 변경·사용자 중지로 DB에서 먼저 cancelled) 잡 상태는
        바꿀 수 없으므로 자동발송 보안 정지(paused_security)만 따로 건다(N-3 — 신호 보존).
        """
        disallowed = [t for t in report.tools_used if t not in ALLOWED_TOOLS_AUTO_REPLY_DRAFT]
        if disallowed:
            reason = "security_tool"
            validation: dict[str, Any] = {"disallowed_tools": disallowed[:10]}
        elif report.submit_count > 1:
            reason = "security_submit_count"
            validation = {"submit_count": report.submit_count}
        else:
            return None
        failed = autoreply_repo.fail_running_job(
            self._db,
            job_id=job.id,
            error_class="security",
            reason=reason,
            now=self._clock.now(),
            discard=True,
            pause_security=True,
            validation=validation,
        )
        if not failed:
            logger.error(
                "이미 종료된 잡에서 보안 신호(%s) — 자동발송을 보안 정지합니다: job_id=%s",
                reason,
                job.id,
            )
            autoreply_repo.pause_autosend_for_security(self._db)
        return f"failed:{reason}"

    def _stored_submission_hash(self, job_id: int) -> str | None:
        conn = self._db.new_read_connection()
        try:
            row = autoreply_repo.get_job(conn, job_id)
        finally:
            conn.close()
        return row.submission_sha256 if row is not None else None

    def _commit(
        self,
        job: AutoReplyJobRow,
        facts: Any,
        submission: Submission,
        subject: str,
        max_chars: int,
        preflight: PreflightResult,
    ) -> str:
        decision = "skip" if submission.decision == "skip" else "reply"
        body = "" if decision == "skip" else submission.body_text
        guard = self._guard(
            body=body,
            subject=subject,
            original_body=facts.body_text,
            original_subject=facts.subject,
            max_chars=max_chars,
        )
        to_norm = facts.from_addr_norm or ""
        proposed = ProposedReply(
            decision=decision,  # type: ignore[arg-type]
            body_text=body,
            reason=submission.reason,
            to_addr=facts.from_addr,
            to_addr_norm=to_norm,
            to_domain_norm=_domain(to_norm),
            subject=subject,
        )
        outcome: CommitOutcome = self._repo.commit_autoreply_result(
            job_id=job.id,
            proposed=proposed,
            guard=guard,
            preflight=preflight,
            now=self._clock.now(),
        )
        return f"{outcome.outcome}:{outcome.reason_code or ''}"
