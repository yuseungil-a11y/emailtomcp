"""G7 권위 판정 — `core.ports.AutoSendVerifier`의 유일한 프로덕션 구현 (§7.9, §7.11 M-D).

app.py가 `AutoReplyRepository(db, verifier=AutoSendVerifierImpl())`로 한 번 주입한다(아키텍처
테스트: 구현·주입·`AutoSendVerdict(` 생성 위치 제한). G7 writer 트랜잭션 안에서 같은 conn으로
고정 순서로 판정한다.

**Phase B 구현 — 자동발송을 절대 허용하지 않는다**
`AutoSendVerdict.decision`에는 자동발송 값이 없고(core/autoreply_types.py), 이 구현은 판정이
통과해도 언제나 `draft`를 돌려준다. 규칙 action이 auto_send였다면 G2가 이미 계획을 draft로
강등했고(사유 `autosend_not_yet_supported`), 여기서도 같은 사유를 다시 붙인다.

판정 순서
1. gate(§7.0): 토글·세대·계정·규칙 enabled·rule_version → 불통과면 `cancel`(초안 없음, R-i·R-j)
2. 사전점검 바인딩(A8): job_id·attempt 일치, `started_at ≤ pre ≤ post`(post는 미래가 아님) →
   어긋나면 `discard`(보안)
3. 본문 동일성(⑦): GuardResult 해시 = 트랜잭션에서 읽은 submission_sha256, claude 실행 잡이면
   PreflightResult.verified_body_sha256도 같아야 함 → 아니면 `discard`(보안)
3-1. G6 사실 재확인(Spinoza 34번 L-5, 러너와 2중 방어): 트랜잭션에서 G5 제출 기록
   (status=running·submitted_at·submission_sha256)을 다시 읽어 대조하고, PreflightResult에 기록된
   G6 값이 `tools_used ⊆ 허용 집합`(고정 템플릿은 빈 집합)·`submit_count == 1`·(claude 실행 잡)
   `exit_code == 0`인지 확인 → 아니면 `discard`(보안)
4. 사후 점검 강제(A8, Spinoza 30번 M-2) — claude 실행 잡만:
   - 실행 중 user/managed 설정·`~/.claude/CLAUDE.md`가 바뀌었거나(`user_settings_unchanged`
     False) 잡 디렉터리에 예상 밖 파일이 생겼으면 → `discard`(보안)
   - 상위 경로에 claude 설정 표식이 있거나(`ancestor_clean` False) 잡 디렉터리 DACL이 실패했으면
     → `cancel`(설계상 기준은 draft 강등이지만 fallback3과 정책을 맞춰 초안을 만들지 않는다)
5. 설정 격리 폴백3(M-E): claude 실행 잡이면 Phase B는 **초안도 만들지 않고** `cancel`
   (설계서는 auto_send만 금지하지만, 사용자 hooks·플러그인이 잡 토큰을 가진 채 돌았을 수 있어
   이번 단계는 더 보수적으로 간다. 러너도 실행 전에 같은 이유로 취소한다 — 이것은 2중 방어)
6. 그 밖에는 `draft`
"""

from __future__ import annotations

import hmac
import sqlite3
from datetime import UTC, datetime

from emailtomcp.core.autoreply_types import (
    AUTOREPLY_DRAFT_ALLOWED_TOOLS,
    AutoReplyJobRow,
    AutoSendVerdict,
    GuardResult,
    PreflightResult,
)
from emailtomcp.rules.gate import check_autoreply_gate

REASON_AUTOSEND_NOT_YET = "autosend_not_yet_supported"
REASON_PREFLIGHT_BINDING = "preflight_binding_mismatch"
REASON_BODY_HASH = "body_hash_mismatch"
REASON_ISOLATION_FALLBACK3 = "isolation_fallback3"
REASON_SETTINGS_CHANGED = "preflight_settings_changed"
REASON_UNEXPECTED_FILES = "preflight_unexpected_files"
REASON_ANCESTOR_UNCLEAN = "preflight_ancestor_unclean"
REASON_JOBDIR_ACL = "preflight_jobdir_acl"
# G6 사실 재확인(Spinoza 34번 L-5) — 러너가 이미 걸렀어야 하므로 여기서 걸리면 보안 폐기.
REASON_G6_TOOLS = "g6_tools_mismatch"
REASON_G6_SUBMIT_COUNT = "g6_submit_count_mismatch"
REASON_G6_EXIT_CODE = "g6_exit_code_mismatch"
REASON_G6_SUBMISSION = "g6_submission_missing"


def _submission_recorded(conn: sqlite3.Connection, job_id: int, stored: str) -> bool:
    """G5 제출 기록(submitted_at·submission_sha256)을 이 트랜잭션에서 **직접** 다시 읽어 대조한다.

    저장소가 넘긴 `job` 행에만 기대지 않는다(2중 방어). G5는 `submitted_at IS NULL` 조건부
    UPDATE라 DB 쪽 제출 기록은 잡당 많아야 1건이다.
    """
    row = conn.execute(
        "SELECT status, submitted_at, submission_sha256 FROM auto_reply_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None or row[0] != "running" or not row[1]:
        return False
    return hmac.compare_digest(row[2] or "", stored)


def _g6_mismatch(preflight: PreflightResult) -> str | None:
    """G6 사후 검증 사실(PreflightResult에 기록된 값)을 판정기 쪽에서 다시 확인한다(L-5).

    러너(`job_runner._run_claude`)가 이미 확인하는 조건이지만, 러너 회귀·우회로 이 값이 어긋난 채
    G7에 도달하면 초안을 만들지 않는다.
    - 사용 도구 ⊆ 허용 집합(고정 템플릿 잡은 빈 집합이어야 한다)
    - submit 정확히 1회
    - claude 실행 잡은 종료 코드 0
    """
    allowed = AUTOREPLY_DRAFT_ALLOWED_TOOLS if preflight.claude_executed else ()
    if any(tool not in allowed for tool in preflight.tools_used):
        return REASON_G6_TOOLS
    if preflight.submit_count != 1:
        return REASON_G6_SUBMIT_COUNT
    if preflight.claude_executed and preflight.exit_code != 0:
        return REASON_G6_EXIT_CODE
    return None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _binding_ok(job: AutoReplyJobRow, preflight: PreflightResult, now: datetime) -> bool:
    if preflight.job_id != job.id or preflight.attempt != job.attempts:
        return False
    started = _parse(job.started_at)
    pre = _parse(preflight.pre_checked_at)
    post = _parse(preflight.post_checked_at)
    if started is None or pre is None or post is None:
        return False
    return started <= pre <= post <= now


class AutoSendVerifierImpl:
    """Phase B 판정기. 통과하면 언제나 draft(자동발송 없음)."""

    def __init__(self, *, now_func=None) -> None:  # noqa: ANN001 — 테스트용 시계 주입
        self._now = now_func or (lambda: datetime.now(UTC))

    def verify(
        self,
        conn: sqlite3.Connection,
        job: AutoReplyJobRow,
        *,
        guard: GuardResult,
        preflight: PreflightResult,
    ) -> AutoSendVerdict:
        def verdict(decision: str, reason: str | None) -> AutoSendVerdict:
            return AutoSendVerdict(
                job_id=job.id,
                generation=job.enable_gen,
                rule_version=job.rule_version,
                decision=decision,  # type: ignore[arg-type]
                reason_code=reason,
            )

        gate = check_autoreply_gate(
            conn,
            account_id=job.account_id,
            rule_id=job.rule_id,
            rule_version=job.rule_version,
            job_generation=job.enable_gen,
        )
        if not gate.ok:
            return verdict("cancel", gate.reason_code)
        if not _binding_ok(job, preflight, self._now()):
            return verdict("discard", REASON_PREFLIGHT_BINDING)
        stored = job.submission_sha256 or ""
        if not stored or not hmac.compare_digest(guard.body_sha256, stored):
            return verdict("discard", REASON_BODY_HASH)
        if not _submission_recorded(conn, job.id, stored):
            return verdict("discard", REASON_G6_SUBMISSION)
        g6 = _g6_mismatch(preflight)
        if g6 is not None:
            return verdict("discard", g6)
        if preflight.claude_executed:
            verified = preflight.verified_body_sha256 or ""
            if not verified or not hmac.compare_digest(verified, stored):
                return verdict("discard", REASON_BODY_HASH)
            if preflight.user_settings_unchanged is not True:
                return verdict("discard", REASON_SETTINGS_CHANGED)
            if preflight.unexpected_files:
                return verdict("discard", REASON_UNEXPECTED_FILES)
            if preflight.ancestor_clean is not True:
                return verdict("cancel", REASON_ANCESTOR_UNCLEAN)
            if preflight.jobdir_acl != "owner_only":
                return verdict("cancel", REASON_JOBDIR_ACL)
            if preflight.isolation_mode == "fallback3":
                return verdict("cancel", REASON_ISOLATION_FALLBACK3)
        if job.planned_action == "auto_send" or job.downgrade_reason == REASON_AUTOSEND_NOT_YET:
            return verdict("draft", REASON_AUTOSEND_NOT_YET)
        return verdict("draft", job.downgrade_reason)
