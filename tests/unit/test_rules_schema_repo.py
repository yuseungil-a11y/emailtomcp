"""규칙 스키마 검증(G0)과 rules 저장소 (DESIGN.md §7.1, §7.6 A5, §11.4) — P3 Phase B."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from _autoreply_helpers import add_rule, eml, enable, make_account, make_db, query, store

from emailtomcp.core.errors import PolicyError
from emailtomcp.rules import schema
from emailtomcp.rules.regex_safe import RegexError, compile_rule_regex
from emailtomcp.storage.repositories import autoreply as autoreply_repo
from emailtomcp.storage.repositories import rules as rules_repo

pytestmark = pytest.mark.unit


def _data(**over: object) -> dict:
    data: dict = {
        "name": "견적",
        "action": "draft",
        "reply_mode": "generated",
        "conditions": {
            "match": "all",
            "conditions": [{"field": "subject", "op": "contains", "value": "견적"}],
        },
        "max_chars": 400,
    }
    data.update(over)
    return data


# ---------------------------------------------------------------- 스키마


def test_phase_b_rejects_auto_send_action() -> None:
    with pytest.raises(schema.RuleSchemaError, match="다음 업데이트"):
        schema.validate_rule(_data(action="auto_send"), allow_auto_send=False)


def test_ignore_and_draft_are_accepted() -> None:
    for action in ("ignore", "draft"):
        v = schema.validate_rule(_data(action=action), allow_auto_send=False)
        assert v.fields["action"] == action


def test_auto_send_requires_sender_whitelist_a5() -> None:
    with pytest.raises(schema.RuleSchemaError, match="발신자"):
        schema.validate_rule(_data(action="auto_send"), allow_auto_send=True)
    any_mode = _data(
        action="auto_send",
        conditions={
            "match": "any",
            "conditions": [{"field": "from_domain", "op": "in_list", "value": ["client.example"]}],
        },
    )
    with pytest.raises(schema.RuleSchemaError):
        schema.validate_rule(any_mode, allow_auto_send=True)  # OR 안의 화이트리스트는 무효
    ok = _data(
        action="auto_send",
        conditions={
            "match": "all",
            "conditions": [
                {"field": "from_domain", "op": "in_list", "value": ["gmail.com", "client.example"]}
            ],
        },
    )
    result = schema.validate_rule(ok, allow_auto_send=True)
    assert any("무료 메일" in w for w in result.warnings)


@pytest.mark.parametrize("pattern", [r"(a)\1", r"a(?=b)", r"(?<=x)y", "x" * 201, ""])
def test_regex_rejects_backrefs_lookaround_and_long(pattern: str) -> None:
    with pytest.raises(RegexError):
        compile_rule_regex(pattern)


def test_regex_is_case_insensitive_re2() -> None:
    compiled = compile_rule_regex(r"invoice\s+#\d+")
    assert compiled.search("Re: INVOICE   #123")


def test_condition_validation_errors() -> None:
    bad = [
        {"field": "nope", "op": "equals", "value": "x"},
        {"field": "subject", "op": "in_list", "value": ["x"]},
        {"field": "received_hour", "op": "between", "value": [9, 9]},
        {"field": "received_hour", "op": "between", "value": [9, 25]},
        {"field": "has_attachment", "op": "equals", "value": "yes"},
        {"field": "from_domain", "op": "in_list", "value": []},
        {"field": "body", "op": "regex", "value": "(a)\\1"},
    ]
    for cond in bad:
        with pytest.raises(schema.RuleSchemaError):
            schema.parse_conditions({"match": "all", "conditions": [cond]})


def test_fixed_template_slots_are_restricted() -> None:
    ok = schema.validate_rule(
        _data(reply_mode="fixed_template", fixed_template="{sender_name_safe}님 감사합니다"),
        allow_auto_send=False,
    )
    assert ok.fields["reply_mode"] == "fixed_template"
    with pytest.raises(schema.RuleSchemaError, match="자리표시자"):
        schema.validate_rule(
            _data(reply_mode="fixed_template", fixed_template="{body} 그대로"),
            allow_auto_send=False,
        )


def test_empty_conditions_warns() -> None:
    v = schema.validate_rule(
        _data(conditions={"match": "all", "conditions": []}), allow_auto_send=False
    )
    assert any("모든 메일" in w for w in v.warnings)


# ---------------------------------------------------------------- 저장소


@pytest.fixture
def db(tmp_path: Path):  # noqa: ANN201
    database = make_db(tmp_path)
    yield database
    database.stop()


def test_insert_update_version_cas(db) -> None:  # noqa: ANN001
    rule_id = add_rule(db)
    conn = db.new_read_connection()
    row = rules_repo.get_rule(conn, rule_id)
    conn.close()
    assert row is not None and row.version == 1 and row.priority == 10
    fields = schema.validate_rule(_data(name="새이름"), allow_auto_send=False).fields
    assert rules_repo.update_rule(db, rule_id, expected_version=1, fields=fields) == 2
    with pytest.raises(PolicyError, match="다른 곳에서"):
        rules_repo.update_rule(db, rule_id, expected_version=1, fields=fields)


def test_enable_toggle_and_priorities_do_not_bump_version(db) -> None:  # noqa: ANN001
    a = add_rule(db, name="A")
    b = add_rule(db, name="B")
    rules_repo.set_rule_enabled(db, a, False)
    rules_repo.set_priorities(db, [b, a])
    conn = db.new_read_connection()
    rows = rules_repo.list_rules(conn)
    conn.close()
    assert [r.id for r in rows] == [b, a]
    assert all(r.version == 1 for r in rows)
    assert not next(r for r in rows if r.id == a).enabled


def test_delete_cancels_active_jobs_first(db, tmp_path: Path) -> None:  # noqa: ANN001
    account = make_account(db)
    rule_id = add_rule(db)
    gen = enable(db)
    mid = store(db, tmp_path / "mail", account, eml())
    job_id = autoreply_repo.create_autoreply_job(
        db,
        message_id=mid,
        rule_id=rule_id,
        rule_version=1,
        planned_action="draft",
        downgrade_reason=None,
        sender_addr_norm="partner@client.example",
        sender_domain="client.example",
        thread_key=None,
        expected_generation=gen,
        now=datetime.now(UTC),
    )
    assert job_id is not None
    assert rules_repo.delete_rule(db, rule_id) is True
    job = query(db, "SELECT status, error, rule_id FROM auto_reply_jobs WHERE id = ?", (job_id,))[0]
    assert job["status"] == "cancelled" and job["error"] == "rule_deleted"
    assert job["rule_id"] is None  # FK를 끊었다(로그에는 rule_id가 남는다)
    log = query(db, "SELECT outcome, rule_id FROM auto_reply_log WHERE job_id = ?", (job_id,))
    assert [tuple(r) for r in log] == [("cancelled", rule_id)]
    assert query(db, "SELECT COUNT(*) FROM rules")[0][0] == 0
