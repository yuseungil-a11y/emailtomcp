"""gate·G3·G4·G5·G7 writer 관문과 Phase B 판정기 (DESIGN.md §7.0, §7.9, §7.11) — P3 Phase B.

핵심 불변식: **이번 단계에서는 어떤 경로로도 `approved_by='policy…auto_send'` 행이 생기지 않고,
G7 결과는 언제나 status='draft'다**(규칙 action이 auto_send여도).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _autoreply_helpers import PARTNER, add_rule, eml, enable, make_account, make_db, query, store

from emailtomcp.autoreply import preflight as preflight_mod
from emailtomcp.core.autoreply_types import (
    AUTOREPLY_DRAFT_ALLOWED_TOOLS as ALLOWED_TOOLS,
)
from emailtomcp.core.autoreply_types import AutoSendVerdict, ProposedReply
from emailtomcp.core.errors import PolicyError
from emailtomcp.core.models import ApprovedBy
from emailtomcp.rules import gate, output_guard
from emailtomcp.rules.autosend_verify import AutoSendVerifierImpl
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import autoreply as autoreply_repo
from emailtomcp.storage.repositories import rules as rules_repo
from emailtomcp.storage.repositories.autoreply import AutoReplyRepository

pytestmark = pytest.mark.unit

AUTO = ApprovedBy.POLICY_AUTO_SEND.value  # 비교용(테스트 파일은 아키텍처 검사 대상 아님)
BODY = "문의 감사합니다. 담당자가 확인 후 회신드리겠습니다."


@pytest.fixture
def env(tmp_path: Path):  # noqa: ANN201
    db = make_db(tmp_path)
    account = make_account(db)
    yield db, account, tmp_path
    db.stop()


def _now() -> datetime:
    return datetime.now(UTC)


def _job(db, account, tmp_path, *, rule_action="draft", rule=True, gen=None, downgrade=None):  # noqa: ANN001, ANN202
    rule_id = add_rule(db, action=rule_action) if rule else None
    generation = gen if gen is not None else enable(db)
    mid = store(db, tmp_path / "mail", account, eml())
    job_id = autoreply_repo.create_autoreply_job(
        db,
        message_id=mid,
        rule_id=rule_id,
        rule_version=1 if rule else None,
        planned_action="draft",
        downgrade_reason=downgrade,
        sender_addr_norm=PARTNER,
        sender_domain="client.example",
        thread_key=None,
        expected_generation=generation,
        now=_now(),
    )
    return job_id, mid, rule_id, generation


def _claim_and_submit(db, body=BODY):  # noqa: ANN001, ANN202
    job = autoreply_repo.claim_next_job(db, now=_now())
    assert job is not None
    assert autoreply_repo.mark_submitted(
        db, job_id=job.id, body_sha256=autoreply_repo.sha256_text(body), now=_now()
    )
    conn = db.new_read_connection()
    job = autoreply_repo.get_job(conn, job.id)
    conn.close()
    return job


def _proposed(body=BODY, *, to=PARTNER, decision="reply") -> ProposedReply:  # noqa: ANN001
    return ProposedReply(
        decision=decision,
        body_text=body,
        reason=None,
        to_addr=to,
        to_addr_norm=to,
        to_domain_norm=to.rpartition("@")[2],
        subject="Re: 견적 문의드립니다",
    )


def _guard(body=BODY):  # noqa: ANN001, ANN202
    return output_guard.check(
        body=body, subject="Re: x", original_body="원문", original_subject="x", max_chars=400
    )


def _repo(db) -> AutoReplyRepository:  # noqa: ANN001
    return AutoReplyRepository(db, verifier=AutoSendVerifierImpl())


# ---------------------------------------------------------------- gate


def test_gate_is_single_shared_function() -> None:
    assert gate.check_autoreply_gate is autoreply_repo.check_autoreply_gate


def test_gate_reasons(env) -> None:  # noqa: ANN001
    db, account, _tmp = env
    rule_id = add_rule(db)

    def check(**kw):  # noqa: ANN003, ANN202
        conn = db.new_read_connection()
        try:
            base = {
                "account_id": account,
                "rule_id": rule_id,
                "rule_version": 1,
                "job_generation": 2,
            }
            base.update(kw)
            return gate.check_autoreply_gate(conn, **base).reason_code
        finally:
            conn.close()

    assert check() == "disabled"  # 토글 off
    gen = enable(db)
    assert gen == 2 and check() is None
    assert check(job_generation=0) == "generation_mismatch"
    assert check(job_generation=None) == "generation_mismatch"
    assert check(job_generation=True) == "generation_mismatch"
    assert check(job_generation=3) == "generation_mismatch"
    assert check(rule_version=2) == "rule_changed"
    assert check(rule_version=None) == "rule_changed"
    assert check(rule_id=999) == "rule_missing"
    assert check(rule_id=None, rule_version=1) == "rule_missing"
    assert check(rule_id=None, rule_version=None) is None
    rules_repo.set_rule_enabled(db, rule_id, False)
    assert check() == "rule_disabled"
    rules_repo.set_rule_enabled(db, rule_id, True)
    accounts_repo.update_account(db, account, auto_reply_enabled=False)
    assert check() == "account_disabled"


# ---------------------------------------------------------------- G3


def test_g3_creates_one_job_per_message_and_rejects_auto_send_plan(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, mid, rule_id, gen = _job(db, account, tmp)
    assert job_id is not None
    again = autoreply_repo.create_autoreply_job(
        db,
        message_id=mid,
        rule_id=rule_id,
        rule_version=1,
        planned_action="draft",
        downgrade_reason=None,
        sender_addr_norm=PARTNER,
        sender_domain="client.example",
        thread_key=None,
        expected_generation=gen,
        now=_now(),
    )
    assert again is None  # 메일당 1잡(멱등)
    with pytest.raises(PolicyError):
        autoreply_repo.create_autoreply_job(
            db,
            message_id=mid,
            rule_id=rule_id,
            rule_version=1,
            planned_action="auto_send",
            downgrade_reason=None,
            sender_addr_norm=PARTNER,
            sender_domain="client.example",
            thread_key=None,
            expected_generation=gen,
            now=_now(),
        )
    assert (
        query(db, "SELECT auto_reply_status FROM messages WHERE id = ?", (mid,))[0][0] == "queued"
    )
    row = query(db, "SELECT enable_gen, planned_action FROM auto_reply_jobs WHERE id=?", (job_id,))[
        0
    ]
    assert tuple(row) == (gen, "draft")


def test_g3_gate_failure_leaves_message_unevaluated(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, mid, _r, _g = _job(db, account, tmp, gen=99)  # 옛/틀린 세대
    assert job_id is None
    assert query(db, "SELECT auto_reply_status FROM messages WHERE id = ?", (mid,))[0][0] is None


def test_g3_job_cap_per_sender_blocks(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    gen = enable(db)
    created = []
    for _ in range(autoreply_repo.JOB_CAP_PER_SENDER_HOURLY + 1):
        mid = store(db, tmp / "mail", account, eml())
        created.append(
            autoreply_repo.create_autoreply_job(
                db,
                message_id=mid,
                rule_id=None,
                rule_version=None,
                planned_action="draft",
                downgrade_reason=None,
                sender_addr_norm=PARTNER,
                sender_domain="client.example",
                thread_key=None,
                expected_generation=gen,
                now=_now(),
            )
        )
    assert all(created[:-1]) and created[-1] is None
    log = query(db, "SELECT outcome, reason_code FROM auto_reply_log ORDER BY id DESC LIMIT 1")[0]
    assert tuple(log) == ("blocked", "job_cap_sender")


# ---------------------------------------------------------------- G4·G5


def test_g4_cancels_jobs_whose_gate_fails_and_respects_pause(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, mid, rule_id, _gen = _job(db, account, tmp)
    autoreply_repo.set_queue_paused(db, paused=True)
    assert autoreply_repo.claim_next_job(db, now=_now()) is None  # 일시정지 → queued 유지
    autoreply_repo.set_queue_paused(db, paused=False)
    rules_repo.set_rule_enabled(db, rule_id, False)  # 실행 전에 규칙 중지(R-i)
    assert autoreply_repo.claim_next_job(db, now=_now()) is None
    row = query(db, "SELECT status, error FROM auto_reply_jobs WHERE id = ?", (job_id,))[0]
    assert tuple(row) == ("cancelled", "rule_disabled")


def test_g5_mark_submitted_only_once_and_only_running(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, *_ = _job(db, account, tmp)
    sha = autoreply_repo.sha256_text(BODY)
    assert not autoreply_repo.mark_submitted(db, job_id=job_id, body_sha256=sha, now=_now())
    autoreply_repo.claim_next_job(db, now=_now())
    assert autoreply_repo.mark_submitted(db, job_id=job_id, body_sha256=sha, now=_now())
    assert not autoreply_repo.mark_submitted(db, job_id=job_id, body_sha256=sha, now=_now())
    with pytest.raises(PolicyError):
        autoreply_repo.mark_submitted(db, job_id=job_id, body_sha256="nothex", now=_now())


# ---------------------------------------------------------------- G7 — draft만


@pytest.mark.parametrize("rule_action", ["draft", "auto_send"])
def test_g7_always_inserts_plain_draft_never_policy_autosend(env, rule_action: str) -> None:  # noqa: ANN001
    """필수 테스트 ①: 규칙 action이 auto_send여도 결과는 draft(approved_by는 자동발송 값 아님)."""
    db, account, tmp = env
    downgrade = "autosend_not_yet_supported" if rule_action == "auto_send" else None
    job_id, mid, _r, _g = _job(db, account, tmp, rule_action=rule_action, downgrade=downgrade)
    job = _claim_and_submit(db)
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    outcome = _repo(db).commit_autoreply_result(
        job_id=job_id, proposed=_proposed(), guard=_guard(), preflight=pre, now=_now()
    )
    assert outcome.outcome == "drafted"
    draft = query(db, "SELECT * FROM drafts WHERE id = ?", (outcome.draft_id,))[0]
    assert draft["status"] == "draft"
    assert draft["approved_by"] is None and draft["approved_by"] != AUTO
    assert draft["approved_at"] is None and draft["outbox_expires_at"] is None
    assert draft["origin"] == "autoreply" and draft["job_id"] == job_id
    assert draft["source_message_id"] == mid and draft["body_text"] == BODY
    assert query(db, "SELECT COUNT(*) FROM drafts WHERE approved_by = ?", (AUTO,))[0][0] == 0
    assert query(db, "SELECT COUNT(*) FROM drafts WHERE status != 'draft'")[0][0] == 0
    log = query(db, "SELECT outcome, reason_code FROM auto_reply_log WHERE job_id = ?", (job_id,))
    assert tuple(log[-1]) == ("drafted", downgrade)
    job_row = query(db, "SELECT status, outcome FROM auto_reply_jobs WHERE id = ?", (job_id,))[0]
    assert tuple(job_row) == ("done", "drafted")
    assert query(db, "SELECT auto_reply_status FROM messages WHERE id=?", (mid,))[0][0] == "drafted"


def test_g7_rejects_when_job_already_cancelled(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, *_ = _job(db, account, tmp)
    job = _claim_and_submit(db)
    autoreply_repo.disable_and_drain(db, now=_now())  # 끄기 → running 잡 cancelled
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    outcome = _repo(db).commit_autoreply_result(
        job_id=job_id, proposed=_proposed(), guard=_guard(), preflight=pre, now=_now()
    )
    assert outcome.outcome == "rejected"
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0
    assert (
        query(db, "SELECT status FROM auto_reply_jobs WHERE id=?", (job_id,))[0][0] == "cancelled"
    )


def test_g7_body_hash_mismatch_is_security_discard(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, *_ = _job(db, account, tmp)
    job = _claim_and_submit(db)
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    other = "다른 본문"
    outcome = _repo(db).commit_autoreply_result(
        job_id=job_id, proposed=_proposed(other), guard=_guard(other), preflight=pre, now=_now()
    )
    assert outcome.outcome == "discarded"
    row = query(
        db, "SELECT status, error_class, outcome FROM auto_reply_jobs WHERE id=?", (job_id,)
    )
    assert tuple(row[0]) == ("failed", "security", "discarded")
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0


def test_g7_recipient_must_be_from_address(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, *_ = _job(db, account, tmp)
    job = _claim_and_submit(db)
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    outcome = _repo(db).commit_autoreply_result(
        job_id=job_id,
        proposed=_proposed(to="attacker@evil.example"),
        guard=_guard(),
        preflight=pre,
        now=_now(),
    )
    assert outcome.outcome == "discarded" and outcome.reason_code == "recipient_mismatch"
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0


def test_g7_gate_change_during_run_cancels_without_draft(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, _mid, rule_id, _g = _job(db, account, tmp)
    job = _claim_and_submit(db)
    rules_repo.set_rule_enabled(db, rule_id, False)  # R-i: 실행 중 규칙 중지
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    outcome = _repo(db).commit_autoreply_result(
        job_id=job_id, proposed=_proposed(), guard=_guard(), preflight=pre, now=_now()
    )
    assert outcome.outcome == "cancelled" and outcome.reason_code == "rule_disabled"
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0


def test_g7_skip_decision_records_skipped(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, mid, _r, _g = _job(db, account, tmp)
    job = _claim_and_submit(db, body="")
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    outcome = _repo(db).commit_autoreply_result(
        job_id=job_id,
        proposed=_proposed("", decision="skip"),
        guard=_guard(""),
        preflight=pre,
        now=_now(),
    )
    assert outcome.outcome == "skipped"
    assert query(db, "SELECT auto_reply_status FROM messages WHERE id=?", (mid,))[0][0] == "skipped"
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0


class _BadVerifier:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def verify(self, conn, job, *, guard, preflight):  # noqa: ANN001, ANN201
        if self.mode == "raise":
            raise RuntimeError("boom")
        if self.mode == "type":
            return {"decision": "draft"}
        return AutoSendVerdict(
            job_id=job.id + 1,
            generation=job.enable_gen,
            rule_version=job.rule_version,
            decision="draft",
            reason_code=None,
        )


@pytest.mark.parametrize(
    ("mode", "error_class"), [("raise", "permanent"), ("type", "security"), ("binding", "security")]
)
def test_g7_verifier_failures_roll_back_and_fail(env, mode: str, error_class: str) -> None:  # noqa: ANN001
    db, account, tmp = env
    job_id, *_ = _job(db, account, tmp)
    job = _claim_and_submit(db)
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    repo = AutoReplyRepository(db, verifier=_BadVerifier(mode))
    outcome = repo.commit_autoreply_result(
        job_id=job_id, proposed=_proposed(), guard=_guard(), preflight=pre, now=_now()
    )
    assert outcome.outcome == "failed"
    row = query(db, "SELECT status, error_class FROM auto_reply_jobs WHERE id=?", (job_id,))[0]
    assert tuple(row) == ("failed", error_class)
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0


def test_repository_requires_verifier(env) -> None:  # noqa: ANN001
    db, _a, _t = env
    with pytest.raises(TypeError):
        AutoReplyRepository(db, verifier=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AutoReplyRepository(db, verifier=object())  # type: ignore[arg-type]


# ---------------------------------------------------------------- 판정기(Phase B)


def test_production_verifier_only_returns_draft_or_safety_decisions(env) -> None:  # noqa: ANN001
    """필수 테스트 ②: 프로덕션 판정기는 통과 시 언제나 draft. 자동발송 판정 자체가 없다."""
    db, account, tmp = env
    _job(db, account, tmp, rule_action="auto_send", downgrade="autosend_not_yet_supported")
    job = _claim_and_submit(db)
    verifier = AutoSendVerifierImpl()
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    conn = db.new_read_connection()
    try:
        verdict = verifier.verify(conn, job, guard=_guard(), preflight=pre)
        assert verdict.decision == "draft"
        assert verdict.reason_code == "autosend_not_yet_supported"
        # planned_action이 (어떻게든) auto_send로 들어와 있어도 draft다.
        forged = dataclasses.replace(job, planned_action="auto_send", downgrade_reason=None)
        assert verifier.verify(conn, forged, guard=_guard(), preflight=pre).decision == "draft"
        # 바인딩·해시가 어긋나면 discard, 폴백3(claude 실행)이면 cancel — 어느 경우도 발송 아님.
        bad_attempt = dataclasses.replace(pre, attempt=pre.attempt + 1)
        assert (
            verifier.verify(conn, job, guard=_guard(), preflight=bad_attempt).decision == "discard"
        )
        assert verifier.verify(conn, job, guard=_guard("x"), preflight=pre).decision == "discard"
        future = dataclasses.replace(pre, post_checked_at=(_now() + timedelta(hours=1)).isoformat())
        assert verifier.verify(conn, job, guard=_guard(), preflight=future).decision == "discard"
        fb3 = dataclasses.replace(
            pre,
            claude_executed=True,
            isolation_mode="fallback3",
            verified_body_sha256=job.submission_sha256,
            exit_code=0,
        )
        assert verifier.verify(conn, job, guard=_guard(), preflight=fb3).decision == "cancel"
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("change", "decision", "reason"),
    [
        ({}, "draft", None),
        ({"user_settings_unchanged": False}, "discard", "preflight_settings_changed"),
        ({"unexpected_files": ("work/x.txt",)}, "discard", "preflight_unexpected_files"),
        ({"ancestor_clean": False}, "cancel", "preflight_ancestor_unclean"),
        ({"jobdir_acl": "failed"}, "cancel", "preflight_jobdir_acl"),
        # 보안 폐기가 cancel보다 먼저(둘 다 어긋나면 discard)
        (
            {"ancestor_clean": False, "user_settings_unchanged": False},
            "discard",
            "preflight_settings_changed",
        ),
    ],
)
def test_verifier_enforces_post_checks(env, change, decision, reason) -> None:  # noqa: ANN001
    """Spinoza 30번 M-2: finalize가 계산한 사후 점검 값을 판정기(G7 ④)가 실제로 강제한다."""
    db, account, tmp = env
    _job(db, account, tmp)
    job = _claim_and_submit(db)
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    executed = dataclasses.replace(
        pre,
        claude_executed=True,
        isolation_mode="user_settings_clean",
        verified_body_sha256=job.submission_sha256,
        tools_used=ALLOWED_TOOLS,
        exit_code=0,
        **change,
    )
    conn = db.new_read_connection()
    try:
        verdict = AutoSendVerifierImpl().verify(conn, job, guard=_guard(), preflight=executed)
    finally:
        conn.close()
    assert (verdict.decision, verdict.reason_code) == (decision, reason)


@pytest.mark.parametrize(
    ("claude", "change", "decision", "reason"),
    [
        (True, {}, "draft", None),
        # 허용 외 도구가 PreflightResult에 남아 있으면 러너를 지나쳤어도 판정기가 폐기
        (True, {"tools_used": (*ALLOWED_TOOLS, "Bash")}, "discard", "g6_tools_mismatch"),
        (True, {"submit_count": 2}, "discard", "g6_submit_count_mismatch"),
        (True, {"submit_count": 0}, "discard", "g6_submit_count_mismatch"),
        (True, {"exit_code": 1}, "discard", "g6_exit_code_mismatch"),
        (True, {"exit_code": None}, "discard", "g6_exit_code_mismatch"),
        # 고정 템플릿(claude 미실행)은 도구가 하나도 없어야 하고 종료 코드는 보지 않는다
        (False, {}, "draft", None),
        (False, {"tools_used": ALLOWED_TOOLS[:1]}, "discard", "g6_tools_mismatch"),
        (False, {"submit_count": 2}, "discard", "g6_submit_count_mismatch"),
    ],
)
def test_verifier_rechecks_g6_facts(env, claude, change, decision, reason) -> None:  # noqa: ANN001
    """Spinoza 34번 L-5: 러너만 보던 G6 사실(도구·submit 횟수·종료 코드)을 판정기도 재확인."""
    db, account, tmp = env
    _job(db, account, tmp)
    job = _claim_and_submit(db)
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())
    base = (
        {
            "claude_executed": True,
            "isolation_mode": "user_settings_clean",
            "verified_body_sha256": job.submission_sha256,
            "tools_used": ALLOWED_TOOLS,
            "exit_code": 0,
        }
        if claude
        else {}
    )
    candidate = dataclasses.replace(pre, **{**base, **change})
    conn = db.new_read_connection()
    try:
        verdict = AutoSendVerifierImpl().verify(conn, job, guard=_guard(), preflight=candidate)
    finally:
        conn.close()
    assert (verdict.decision, verdict.reason_code) == (decision, reason)


def test_verifier_rereads_g5_submission_record(env) -> None:  # noqa: ANN001
    """L-5: 판정기는 넘겨받은 job 행이 아니라 트랜잭션에서 G5 제출 기록을 다시 읽어 대조한다."""
    db, account, tmp = env
    _job(db, account, tmp)
    job = _claim_and_submit(db)
    pre = preflight_mod.for_fixed_template(job_id=job.id, attempt=job.attempts, now=_now())

    def _w(conn):  # noqa: ANN001, ANN202
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE auto_reply_jobs SET submitted_at = NULL, submission_sha256 = NULL WHERE id = ?",
            (job.id,),
        )
        conn.commit()

    db.writer.submit(_w).result()
    conn = db.new_read_connection()
    try:
        # job 객체에는 여전히 제출 기록이 있지만 DB에는 없다 → 폐기
        verdict = AutoSendVerifierImpl().verify(conn, job, guard=_guard(), preflight=pre)
    finally:
        conn.close()
    assert (verdict.decision, verdict.reason_code) == ("discard", "g6_submission_missing")


def test_verdict_decision_type_has_no_autosend_value() -> None:
    import typing

    from emailtomcp.core import autoreply_types

    values = set(typing.get_args(autoreply_types.VerdictDecision))
    assert values == {"draft", "cancel", "discard"}
