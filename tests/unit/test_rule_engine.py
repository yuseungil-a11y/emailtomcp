"""RuleEngine §7.2 평가 순서 0~6단계 + G3 연결, 따라잡기, 드라이런 — P3 Phase B."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _autoreply_helpers import (
    PARTNER,
    add_rule,
    eml,
    enable,
    make_account,
    make_db,
    make_engine,
    query,
    store,
)

from emailtomcp.config.settings import set_setting
from emailtomcp.core.events import EventBus
from emailtomcp.mail.addressing import normalize_addr
from emailtomcp.rules import engine as rule_engine
from emailtomcp.rules.schema import parse_conditions
from emailtomcp.storage.repositories import accounts as accounts_repo

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path: Path):  # noqa: ANN201
    db = make_db(tmp_path)
    account = make_account(db)
    yield db, account, tmp_path / "mail"
    db.stop()


def _status(db, mid):  # noqa: ANN001, ANN202
    return query(db, "SELECT auto_reply_status FROM messages WHERE id = ?", (mid,))[0][0]


def _jobs(db):  # noqa: ANN001, ANN202
    return query(
        db, "SELECT message_id, rule_id, planned_action, downgrade_reason FROM auto_reply_jobs"
    )


# ---------------------------------------------------------------- 0단계: 토글


def test_not_subscribed_while_off_and_no_evaluation(env) -> None:  # noqa: ANN001
    db, account, mail = env
    bus = EventBus()
    engine = make_engine(db, bus)
    assert not engine.subscribed
    mid = store(db, mail, account, eml())
    bus.publish("MessageReceived", {"message_id": mid})
    assert engine.evaluations == 0 and _jobs(db) == [] and _status(db, mid) is None


def test_subscribe_evaluates_and_stop_unsubscribes(env) -> None:  # noqa: ANN001
    db, account, mail = env
    add_rule(db)
    gen = enable(db)
    bus = EventBus()
    woke: list[int] = []
    engine = make_engine(db, bus, on_job=lambda: woke.append(1))
    engine.start(gen)
    mid = store(db, mail, account, eml())
    bus.publish("MessageReceived", {"message_id": mid})
    assert _status(db, mid) == "queued" and woke == [1]
    engine.stop()
    mid2 = store(db, mail, account, eml())
    bus.publish("MessageReceived", {"message_id": mid2})
    assert _status(db, mid2) is None


def test_stale_generation_aborts(env) -> None:  # noqa: ANN001
    db, account, mail = env
    gen = enable(db)
    engine = make_engine(db)
    engine.start(gen)
    mid = store(db, mail, account, eml())
    assert engine.evaluate_message(mid, expected_generation=gen + 5) == "stale"
    assert _status(db, mid) is None


# ---------------------------------------------------------------- 1단계: 평가 제외


def test_exclusions_leave_status_null(env) -> None:  # noqa: ANN001
    db, account, mail = env
    before = store(db, mail, account, eml())  # watermark 이전
    add_rule(db)
    gen = enable(db)
    engine = make_engine(db)
    engine.start(gen)
    cases = {
        "before_watermark": before,
        "backfill": store(db, mail, account, eml(), is_backfill=True),
        "not_inbox": store(db, mail, account, eml(), folder="Archive", role="archive"),
        "loop_headers_missing": store(db, mail, account, eml(), with_headers=False),
        "too_old": store(db, mail, account, eml(date=datetime.now(UTC) - timedelta(hours=80))),
    }
    for reason, mid in cases.items():
        assert engine.evaluate_message(mid, expected_generation=gen) == f"excluded:{reason}"
        assert _status(db, mid) is None
    accounts_repo.update_account(db, account, auto_reply_enabled=False)
    mid = store(db, mail, account, eml())
    assert (
        engine.evaluate_message(mid, expected_generation=gen) == "excluded:account_auto_reply_off"
    )
    assert query(db, "SELECT COUNT(*) FROM auto_reply_log")[0][0] == 0  # 로그 없음


# ---------------------------------------------------------------- 2단계: LoopGuard


def test_loop_guard_blocks_before_rules_and_logs(env) -> None:  # noqa: ANN001
    db, account, mail = env
    add_rule(db)
    gen = enable(db)
    engine = make_engine(db)
    engine.start(gen)
    mid = store(db, mail, account, eml(extra_headers="Auto-Submitted: auto-replied\r\n"))
    assert engine.evaluate_message(mid, expected_generation=gen) == "blocked"
    assert _status(db, mid) == "blocked"
    log = query(db, "SELECT outcome, reason_code, sender_addr_norm FROM auto_reply_log")
    assert [tuple(r) for r in log] == [("blocked", "loop_auto_submitted", PARTNER)]
    assert _jobs(db) == []


# ---------------------------------------------------------------- 3~6단계


def test_first_matching_rule_by_priority_wins(env) -> None:  # noqa: ANN001
    db, account, mail = env
    ignore_id = add_rule(
        db,
        name="무시",
        action="ignore",
        conditions={
            "match": "all",
            "conditions": [{"field": "subject", "op": "contains", "value": "견적"}],
        },
    )
    add_rule(db, name="초안")  # from_domain in_list client.example
    gen = enable(db)
    engine = make_engine(db)
    engine.start(gen)
    mid = store(db, mail, account, eml(subject="견적 요청"))
    assert engine.evaluate_message(mid, expected_generation=gen) == "ignore"
    assert _status(db, mid) == "ignored"
    assert query(db, "SELECT COUNT(*) FROM auto_reply_log")[0][0] == 0  # ignored는 로그 없음
    assert ignore_id


def test_auto_send_rule_is_planned_as_draft_with_reason(env) -> None:  # noqa: ANN001
    db, account, mail = env
    rule_id = add_rule(db, action="auto_send")
    gen = enable(db)
    engine = make_engine(db)
    engine.start(gen)
    mid = store(db, mail, account, eml())
    assert engine.evaluate_message(mid, expected_generation=gen) == "queued"
    assert [tuple(r) for r in _jobs(db)] == [(mid, rule_id, "draft", "autosend_not_yet_supported")]


def test_unmatched_action_default_ignore_and_draft_option(env) -> None:  # noqa: ANN001
    db, account, mail = env
    gen = enable(db)
    engine = make_engine(db)
    engine.start(gen)
    mid = store(db, mail, account, eml())
    assert engine.evaluate_message(mid, expected_generation=gen) == "ignore"
    set_setting(db, rule_engine.SETTING_UNMATCHED_ACTION, "draft")
    mid2 = store(db, mail, account, eml())
    assert engine.evaluate_message(mid2, expected_generation=gen) == "queued"
    assert [tuple(r) for r in _jobs(db)] == [(mid2, None, "draft", None)]


def test_rule_account_scope_and_disabled_rule(env) -> None:  # noqa: ANN001
    db, account, mail = env
    other = make_account(db, email="other@corp.example")
    add_rule(db, account_id=other)  # 다른 계정 전용
    add_rule(db, name="중지됨", enabled=False)
    gen = enable(db)
    engine = make_engine(db)
    engine.start(gen)
    mid = store(db, mail, account, eml())
    assert engine.evaluate_message(mid, expected_generation=gen) == "ignore"


def test_condition_operators() -> None:
    view = rule_engine.MessageView(
        from_norm="kim@client.example",
        from_domain="client.example",
        to_norms=("me@example.com",),
        cc_norms=("boss@example.com",),
        subject="Re: 견적서 송부",
        body="첨부한 견적서를 확인 부탁드립니다",
        has_attachment=True,
        received_local=datetime(2026, 10, 5, 22, 0),  # 월요일 22시
    )

    def match(conds, mode="all"):  # noqa: ANN001, ANN202
        return rule_engine.match_rule(parse_conditions({"match": mode, "conditions": conds}), view)

    assert match([{"field": "from_domain", "op": "in_list", "value": ["CLIENT.example"]}])
    assert match([{"field": "subject", "op": "regex", "value": r"견적서\s+송부"}])
    assert match([{"field": "body", "op": "not_contains", "value": "계좌"}])
    assert match([{"field": "cc", "op": "ends_with", "value": "@example.com"}])
    assert match([{"field": "has_attachment", "op": "equals", "value": True}])
    assert match([{"field": "received_hour", "op": "between", "value": [18, 9]}])  # 자정 넘김
    assert not match([{"field": "received_hour", "op": "between", "value": [9, 18]}])
    assert match([{"field": "received_weekday", "op": "equals", "value": 0}])
    assert not match(
        [
            {"field": "subject", "op": "contains", "value": "청구"},
            {"field": "from_domain", "op": "equals", "value": "client.example"},
        ]
    )
    assert match(
        [
            {"field": "subject", "op": "contains", "value": "청구"},
            {"field": "from_domain", "op": "equals", "value": "client.example"},
        ],
        "any",
    )
    assert not match([{"field": "auth", "op": "equals", "value": "dmarc_pass"}])  # Phase B 불일치


# ---------------------------------------------------------------- 따라잡기·드라이런


def test_catch_up_evaluates_unevaluated_after_watermark_once(env) -> None:  # noqa: ANN001
    db, account, mail = env
    add_rule(db)
    old = store(db, mail, account, eml())
    gen = enable(db)
    new1 = store(db, mail, account, eml())
    new2 = store(db, mail, account, eml())
    engine = make_engine(db)
    engine.start(gen)
    assert engine.catch_up(expected_generation=gen) == 2
    assert engine.catch_up(expected_generation=gen) == 0  # 이미 평가됨(멱등)
    assert _status(db, old) is None and _status(db, new1) == "queued"
    assert _status(db, new2) == "queued"
    assert len(_jobs(db)) == 2


def test_dry_run_counts_without_writing(env) -> None:  # noqa: ANN001
    db, account, mail = env
    store(db, mail, account, eml())
    store(db, mail, account, eml(frm="other@else.example"))
    store(db, mail, account, eml(extra_headers="Precedence: bulk\r\n"))
    store(db, mail, account, eml(), with_headers=False)  # 이전 버전 메일 → 폴백 사용
    conn = db.new_read_connection()
    try:
        report = rule_engine.dry_run_rule(
            conn,
            conditions=parse_conditions(
                {
                    "match": "all",
                    "conditions": [
                        {"field": "from_domain", "op": "in_list", "value": ["client.example"]}
                    ],
                }
            ),
            action="auto_send",
            account_id=None,
            normalize=normalize_addr,
            headers_fallback=lambda _p: {
                "v": 1,
                "is_backfill": False,
                "headers": {},
                "content_type": "text/plain",
                "report_parts": False,
            },
        )
    finally:
        conn.close()
    assert report.total == 4 and report.matched == 3 and report.blocked == 1
    assert report.would_draft == 2  # auto_send 규칙도 이번 버전은 초안
    assert any(r.note for r in report.rows)
    assert query(db, "SELECT COUNT(*) FROM auto_reply_jobs")[0][0] == 0
    assert query(db, "SELECT COUNT(*) FROM messages WHERE auto_reply_status IS NOT NULL")[0][0] == 0
