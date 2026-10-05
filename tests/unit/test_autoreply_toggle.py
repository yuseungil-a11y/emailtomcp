"""P3 Phase A — 자동회신 전역 토글 레포지토리·컨트롤러 테스트 (DESIGN.md §7.0, §7.9, §7.11).

- L-A: 끄기는 CAS 없이 항상 성공, 켜기는 compare-and-set.
- L-B: 세대는 늘어나기만 하고, 세대가 1 미만이거나 손상이면 유효 off(fail-closed).
- 끄기 1단계(a~e)가 하나의 트랜잭션이다(중간 실패 시 전부 롤백).
- 기동 복구: off면 cancelled/draft, on이면 running→queued(enable_gen 유지).
- I8: policy_auto_send outbox가 draft로 돌아가면 approved_by·approved_at·outbox_expires_at이 NULL.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _helpers import make_account, make_db

from emailtomcp.autoreply.controller import (
    EVENT_AUTOREPLY_ENABLED_CHANGED,
    AutoReplyController,
    ControllerState,
)
from emailtomcp.core.clock import SystemClock
from emailtomcp.core.errors import PolicyError
from emailtomcp.core.events import EventBus
from emailtomcp.storage.db import Database
from emailtomcp.storage.repositories import autoreply as autoreply_repo

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)
TS = "2026-10-05T05:00:00+00:00"


# ---------------------------------------------------------------- 픽스처 헬퍼


def _state(db: Database) -> autoreply_repo.ToggleState:
    conn = db.new_read_connection()
    try:
        return autoreply_repo.get_toggle_state(conn)
    finally:
        conn.close()


def _query(db: Database, sql: str, params: tuple = ()) -> list:
    conn = db.new_read_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _exec(db: Database, sql: str, params: tuple = ()) -> int:
    """테스트 데이터 직접 삽입(writer 경유). lastrowid를 돌려준다."""

    def _w(conn):  # noqa: ANN001, ANN202
        conn.execute("BEGIN")
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid

    return db.writer.submit(_w).result()


def _raw_setting(db: Database, key: str) -> object:
    rows = _query(db, "SELECT value_json FROM settings WHERE key = ?", (key,))
    return json.loads(rows[0][0]) if rows else None


def _set_raw(db: Database, key: str, raw_json: str) -> None:
    _exec(
        db,
        "INSERT INTO settings(key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, raw_json),
    )


def _make_message(db: Database, account_id: int, *, status: str | None = None) -> int:
    folder = _query(db, "SELECT id FROM folders WHERE account_id = ? LIMIT 1", (account_id,))
    if folder:
        folder_id = folder[0][0]
    else:
        folder_id = _exec(
            db,
            "INSERT INTO folders (account_id, name, role) VALUES (?, 'INBOX', 'inbox')",
            (account_id,),
        )
    return _exec(
        db,
        "INSERT INTO messages (account_id, folder_id, received_at, eml_path, auto_reply_status) "
        "VALUES (?, ?, ?, 'x.eml', ?)",
        (account_id, folder_id, TS, status),
    )


def _make_job(
    db: Database, message_id: int, *, status: str, kind: str = "auto_reply", enable_gen: int = 2
) -> int:
    return _exec(
        db,
        "INSERT INTO auto_reply_jobs (kind, message_id, planned_action, status, created_at, "
        "enable_gen, sender_addr_norm) VALUES (?, ?, 'auto_send', ?, ?, ?, 'a@ex.com')",
        (kind, message_id, status, TS, enable_gen),
    )


def _make_draft(
    db: Database,
    account_id: int,
    *,
    status: str,
    approved_by: str | None,
    origin: str = "autoreply",
    job_id: int | None = None,
) -> int:
    return _exec(
        db,
        "INSERT INTO drafts (account_id, kind, origin, status, approved_by, approved_at, "
        "outbox_expires_at, job_id, created_at, updated_at) "
        "VALUES (?, 'reply', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            origin,
            status,
            approved_by,
            TS if approved_by else None,
            TS if approved_by == "policy_auto_send" else None,
            job_id,
            TS,
            TS,
        ),
    )


def _draft(db: Database, draft_id: int):  # noqa: ANN202
    return _query(db, "SELECT * FROM drafts WHERE id = ?", (draft_id,))[0]


@pytest.fixture
def db(tmp_path: Path):  # noqa: ANN201
    database = make_db(tmp_path)
    yield database
    database.stop()


# ---------------------------------------------------------------- 초기 상태 / 세대 규칙


def test_fresh_db_is_off_with_generation_one(db: Database) -> None:
    state = _state(db)
    assert state.enabled is False  # I7: 키가 없으면 off
    assert state.generation == 1  # m0003 초기값
    assert state.watermark_message_id == 0
    assert state.enabled_changed_at is None
    assert _raw_setting(db, autoreply_repo.SETTING_ENABLED) is None


def test_enable_cas_success_sets_generation_watermark_and_changed_at(db: Database) -> None:
    account_id = make_account(db)
    _make_message(db, account_id)
    last_id = _make_message(db, account_id)

    new_gen = autoreply_repo.enable(db, expected_generation=1, now=NOW)

    assert new_gen == 2
    state = _state(db)
    assert state.enabled is True
    assert state.generation == 2
    assert state.watermark_message_id == last_id
    assert state.enabled_changed_at == NOW.isoformat()
    # P3 Phase B: queue_state가 한 번도 정해진 적 없으면(m0003이 지움) 첫 켜기에서만 running으로
    # 초기화한다(G4가 queue_state=running을 요구). autosend_state는 만들지 않는다(자동발송 없음).
    assert _raw_setting(db, autoreply_repo.SETTING_QUEUE_STATE) == "running"
    assert _raw_setting(db, autoreply_repo.SETTING_AUTOSEND_STATE) is None


def test_enable_keeps_existing_queue_state(db: Database) -> None:
    """이미 정해진 queue_state(일시정지 등)는 켜기가 바꾸지 않는다(§7.0 켜기 2단계)."""
    autoreply_repo.enable(db, expected_generation=1, now=NOW)
    autoreply_repo.set_queue_paused(db, paused=True)
    autoreply_repo.disable_and_drain(db, now=NOW)
    autoreply_repo.enable(db, expected_generation=3, now=NOW)
    assert _raw_setting(db, autoreply_repo.SETTING_QUEUE_STATE) == "paused_user"


def test_enable_with_stale_generation_is_rejected_without_change(db: Database) -> None:
    with pytest.raises(PolicyError, match="다른 곳에서 바뀌었습니다"):
        autoreply_repo.enable(db, expected_generation=0, now=NOW)
    state = _state(db)
    assert state.enabled is False
    assert state.generation == 1


def test_enable_when_already_on_is_rejected(db: Database) -> None:
    gen = autoreply_repo.enable(db, expected_generation=1, now=NOW)
    with pytest.raises(PolicyError):
        autoreply_repo.enable(db, expected_generation=gen, now=NOW)
    assert _state(db).generation == gen


def test_enable_rejects_bool_expected_generation(db: Database) -> None:
    with pytest.raises(PolicyError):
        autoreply_repo.enable(db, expected_generation=True, now=NOW)  # type: ignore[arg-type]
    assert _state(db).enabled is False


def test_disable_always_succeeds_regardless_of_generation(db: Database) -> None:
    """L-A: 끄기에는 expected_generation 자체가 없고, 몇 번을 불러도 성공한다."""
    autoreply_repo.enable(db, expected_generation=1, now=NOW)  # gen 2
    # 다른 경로에서 세대가 바뀐 상황을 흉내 낸다
    _set_raw(db, autoreply_repo.SETTING_GENERATION, "7")

    report = autoreply_repo.disable_and_drain(db, now=NOW)
    assert report.generation == 8
    assert _state(db).enabled is False

    report2 = autoreply_repo.disable_and_drain(db, now=NOW)  # 이미 꺼져 있어도 성공
    assert report2.generation == 9
    assert _state(db).enabled is False


def test_aba_old_expected_generation_cannot_reenable(db: Database) -> None:
    """R-g/R-h: 껐다 켜는 사이 세대가 늘어 옛 기대값으로는 켤 수 없다."""
    gen_on = autoreply_repo.enable(db, expected_generation=1, now=NOW)  # 2
    report = autoreply_repo.disable_and_drain(db, now=NOW)  # 3
    assert report.generation == gen_on + 1
    with pytest.raises(PolicyError):
        autoreply_repo.enable(db, expected_generation=1, now=NOW)
    with pytest.raises(PolicyError):
        autoreply_repo.enable(db, expected_generation=gen_on, now=NOW)
    assert autoreply_repo.enable(db, expected_generation=3, now=NOW) == 4


def test_generation_only_increases(db: Database) -> None:
    seen = [_state(db).generation]
    for _ in range(3):
        seen.append(autoreply_repo.enable(db, expected_generation=seen[-1], now=NOW))
        seen.append(autoreply_repo.disable_and_drain(db, now=NOW).generation)
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)


@pytest.mark.parametrize("raw", ['"abc"', "true", "-1", "1.5", "{bad json"])
def test_corrupt_generation_is_fail_closed(db: Database, raw: str) -> None:
    autoreply_repo.enable(db, expected_generation=1, now=NOW)
    _set_raw(db, autoreply_repo.SETTING_GENERATION, raw)

    state = _state(db)
    assert state.enabled is False  # 저장값이 true여도 유효 off
    assert state.generation is None
    with pytest.raises(PolicyError):
        autoreply_repo.enable(db, expected_generation=1, now=NOW)

    report = autoreply_repo.disable_and_drain(db, now=NOW)  # 끄기는 성공
    assert report.generation is None
    assert _raw_setting(db, autoreply_repo.SETTING_ENABLED) is False


def test_stored_true_with_generation_zero_is_effectively_off(db: Database) -> None:
    """L-B ①: ON 상태의 세대는 항상 1 이상 — 저장값만 true인 상태는 off로 본다.

    세대 0은 m0003 이후 정상 흐름에서 생기지 않으므로 손상으로 보고 켜기도 거부한다(D1 —
    0에서 켜면 세대가 1부터 다시 시작되는 재설정 경로가 된다).
    """
    _set_raw(db, autoreply_repo.SETTING_ENABLED, "true")
    _set_raw(db, autoreply_repo.SETTING_GENERATION, "0")
    state = _state(db)
    assert state.enabled is False
    assert state.generation is None
    with pytest.raises(PolicyError):
        autoreply_repo.enable(db, expected_generation=0, now=NOW)
    assert _raw_setting(db, autoreply_repo.SETTING_GENERATION) == 0  # 그대로


def test_missing_generation_key_is_corrupt_and_enable_is_rejected(db: Database) -> None:
    """D1 회귀: 세대를 올린 뒤 generation 행을 지우고 켜도 세대가 1부터 다시 시작되지 않는다."""
    gen = autoreply_repo.enable(db, expected_generation=1, now=NOW)  # 2
    gen = autoreply_repo.disable_and_drain(db, now=NOW).generation  # 3
    assert gen == 3
    _exec(db, "DELETE FROM settings WHERE key = ?", (autoreply_repo.SETTING_GENERATION,))

    state = _state(db)
    assert state.enabled is False
    assert state.generation is None  # 키 없음 = 손상(0으로 읽지 않는다)
    for expected in (0, 1, 3):
        with pytest.raises(PolicyError):
            autoreply_repo.enable(db, expected_generation=expected, now=NOW)
    assert _raw_setting(db, autoreply_repo.SETTING_GENERATION) is None
    assert _state(db).enabled is False


def test_disable_does_not_recreate_missing_generation_key(db: Database) -> None:
    """D1 회귀(끄기 쪽): 키가 없을 때 끄기가 0+1=1을 써 넣어 세대를 재설정하지 않는다."""
    _exec(db, "DELETE FROM settings WHERE key = ?", (autoreply_repo.SETTING_GENERATION,))
    report = autoreply_repo.disable_and_drain(db, now=NOW)
    assert report.generation is None
    assert _raw_setting(db, autoreply_repo.SETTING_GENERATION) is None
    assert _raw_setting(db, autoreply_repo.SETTING_ENABLED) is False
    with pytest.raises(PolicyError):
        autoreply_repo.enable(db, expected_generation=1, now=NOW)


def test_first_enable_on_fresh_db_still_works_after_d1(db: Database) -> None:
    """D1 수정이 정상 DB의 최초 켜기(m0003 초기값 1 → 2)를 막지 않는다."""
    assert _raw_setting(db, autoreply_repo.SETTING_GENERATION) == 1
    assert autoreply_repo.enable(db, expected_generation=1, now=NOW) == 2
    assert _state(db).enabled is True


@pytest.mark.parametrize("raw", ["1", '"true"', "null", "{bad"])
def test_enabled_must_be_exact_json_true(db: Database, raw: str) -> None:
    _set_raw(db, autoreply_repo.SETTING_ENABLED, raw)
    assert _state(db).enabled is False


# ---------------------------------------------------------------- 끄기 드레인(§7.0 1b~1e)


def test_disable_drains_jobs_outbox_and_logs_in_one_transaction(db: Database) -> None:
    account_id = make_account(db)
    m_queued = _make_message(db, account_id, status="queued")
    m_running = _make_message(db, account_id, status="running")
    m_summary = _make_message(db, account_id)
    m_done = _make_message(db, account_id, status="sent")
    j_queued = _make_job(db, m_queued, status="queued")
    j_running = _make_job(db, m_running, status="running")
    j_summary = _make_job(db, m_summary, status="queued", kind="summary")
    j_done = _make_job(db, m_done, status="done")

    d_policy = _make_draft(
        db, account_id, status="outbox", approved_by="policy_auto_send", job_id=j_done
    )
    d_user = _make_draft(db, account_id, status="outbox", approved_by="user_send")
    d_mcp = _make_draft(db, account_id, status="outbox", approved_by="ui", origin="mcp")
    _make_draft(db, account_id, status="sending", approved_by="policy_auto_send")

    autoreply_repo.enable(db, expected_generation=1, now=NOW)
    report = autoreply_repo.disable_and_drain(db, now=NOW)

    assert report.cancelled_jobs == 2
    assert report.cancelled_running_job_ids == (j_running,)
    assert report.reverted_outbox == 1
    assert report.sending_in_flight == 1

    jobs = {
        row["id"]: row
        for row in _query(db, "SELECT id, status, error, error_class FROM auto_reply_jobs")
    }
    assert jobs[j_queued]["status"] == "cancelled"
    assert jobs[j_queued]["error"] == "disabled"
    assert jobs[j_running]["status"] == "cancelled"
    assert jobs[j_summary]["status"] == "queued"  # 수동 요약은 영향 없음(D-12 잠정)
    assert jobs[j_done]["status"] == "done"

    msgs = {
        row["id"]: row["auto_reply_status"]
        for row in _query(db, "SELECT id, auto_reply_status FROM messages")
    }
    assert msgs[m_queued] == "cancelled"
    assert msgs[m_running] == "cancelled"
    assert msgs[m_done] == "sent"

    logs = _query(db, "SELECT job_id, outcome, reason_code FROM auto_reply_log ORDER BY job_id")
    assert [(r["job_id"], r["outcome"], r["reason_code"]) for r in logs] == [
        (j_queued, "cancelled", "disabled"),
        (j_running, "cancelled", "disabled"),
    ]

    policy = _draft(db, d_policy)
    assert policy["status"] == "draft"
    assert policy["approved_by"] is None  # I8
    assert policy["approved_at"] is None
    assert policy["outbox_expires_at"] is None
    assert _draft(db, d_user)["status"] == "outbox"  # 사람이 보낸 건 건드리지 않음
    assert _draft(db, d_user)["approved_by"] == "user_send"
    assert _draft(db, d_mcp)["status"] == "outbox"


def test_disable_rolls_back_everything_on_midway_failure(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """끄기 1단계는 하나의 트랜잭션 — 1c에서 실패하면 1a·1b도 반영되지 않는다."""
    account_id = make_account(db)
    m = _make_message(db, account_id, status="queued")
    job = _make_job(db, m, status="queued")
    gen = autoreply_repo.enable(db, expected_generation=1, now=NOW)

    def _boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("injected")

    monkeypatch.setattr(autoreply_repo, "drain_autosend_outbox", _boom)
    with pytest.raises(RuntimeError):
        autoreply_repo.disable_and_drain(db, now=NOW)

    state = _state(db)
    assert state.enabled is True
    assert state.generation == gen
    assert _query(db, "SELECT status FROM auto_reply_jobs WHERE id=?", (job,))[0][0] == "queued"
    assert _query(db, "SELECT COUNT(*) FROM auto_reply_log")[0][0] == 0


def test_drain_autosend_outbox_filters_by_account_and_rule(db: Database) -> None:
    a1 = make_account(db)
    a2 = make_account(db, email_address="other@example.com")
    rule_a = _exec(
        db,
        "INSERT INTO rules (name, conditions_json, action, created_at, updated_at) "
        "VALUES ('r', '[]', 'auto_send', ?, ?)",
        (TS, TS),
    )
    m1 = _make_message(db, a1, status="sent")
    j1 = _make_job(db, m1, status="done")
    _exec(db, "UPDATE auto_reply_jobs SET rule_id = ? WHERE id = ?", (rule_a, j1))
    rule_b = _exec(
        db,
        "INSERT INTO rules (name, conditions_json, action, created_at, updated_at) "
        "VALUES ('r2', '[]', 'auto_send', ?, ?)",
        (TS, TS),
    )
    m2 = _make_message(db, a1, status="sent")
    j2 = _make_job(db, m2, status="done")
    _exec(db, "UPDATE auto_reply_jobs SET rule_id = ? WHERE id = ?", (rule_b, j2))
    d_a1_rule = _make_draft(db, a1, status="outbox", approved_by="policy_auto_send", job_id=j1)
    d_a1_other_rule = _make_draft(
        db, a1, status="outbox", approved_by="policy_auto_send", job_id=j2
    )
    d_a1_norule = _make_draft(db, a1, status="outbox", approved_by="policy_auto_send")
    d_a2 = _make_draft(db, a2, status="outbox", approved_by="policy_auto_send")

    def _drain(**kw):  # noqa: ANN003, ANN202
        def _w(conn):  # noqa: ANN001, ANN202
            conn.execute("BEGIN")
            n = autoreply_repo.drain_autosend_outbox(conn, **kw)
            conn.commit()
            return n

        return db.writer.submit(_w).result()

    # 규칙 범위 드레인: 그 규칙의 행 + 규칙을 알 수 없는 고아 행(job_id NULL, L-6)
    assert _drain(account_id=a1, rule_id=rule_a, reason="rule_saved") == 2
    assert _draft(db, d_a1_rule)["status"] == "draft"
    assert _draft(db, d_a1_norule)["status"] == "draft"
    assert _draft(db, d_a1_other_rule)["status"] == "outbox"  # 다른 규칙은 건드리지 않음
    assert _drain(account_id=a1, rule_id=None, reason="account_saved") == 1
    assert _draft(db, d_a1_other_rule)["status"] == "draft"
    assert _draft(db, d_a2)["status"] == "outbox"
    assert _drain(account_id=None, rule_id=None, reason="disabled") == 1
    assert _draft(db, d_a2)["approved_by"] is None


def test_drain_by_rule_includes_job_without_rule_id(db: Database) -> None:
    """L-6: 잡은 있지만 rule_id가 NULL(규칙 삭제 등)인 행도 규칙 범위 드레인에서 빠지지 않는다."""
    a1 = make_account(db)
    rule_a = _exec(
        db,
        "INSERT INTO rules (name, conditions_json, action, created_at, updated_at) "
        "VALUES ('r', '[]', 'auto_send', ?, ?)",
        (TS, TS),
    )
    m1 = _make_message(db, a1, status="sent")
    j1 = _make_job(db, m1, status="done")  # rule_id NULL
    d = _make_draft(db, a1, status="outbox", approved_by="policy_auto_send", job_id=j1)

    def _w(conn):  # noqa: ANN001, ANN202
        conn.execute("BEGIN")
        n = autoreply_repo.drain_autosend_outbox(
            conn, account_id=None, rule_id=rule_a, reason="rule_saved"
        )
        conn.commit()
        return n

    assert db.writer.submit(_w).result() == 1
    assert _draft(db, d)["status"] == "draft"
    assert _draft(db, d)["approved_by"] is None


def test_account_save_drains_only_when_trust_value_actually_changes(db: Database) -> None:
    """Spinoza 34번 L-2: 신뢰 근거 값이 실제로 바뀐 경우에만 드레인(전체 저장 가용성)."""
    from emailtomcp.storage.repositories import accounts as accounts_repo

    a1 = make_account(db)

    def _outbox() -> int:
        return _make_draft(db, a1, status="outbox", approved_by="policy_auto_send")

    # 같은 값 그대로 다시 저장(계정창 전체 저장) — bool은 0/1 정규화해 비교
    d = _outbox()
    accounts_repo.update_account(
        db,
        a1,
        display_name="이름만 바꿈",
        in_host="imap.example.com",
        incoming_protocol="imap",
        out_host="smtp.example.com",
        out_username=None,
        in_security="ssl",
        out_security="ssl",
        signature=None,
        auto_reply_enabled=False,
        enabled=True,
    )
    assert _draft(db, d)["status"] == "outbox"

    # 새로 추가된 신뢰 필드 각각 — 값이 바뀌면 드레인
    for field, value in (
        ("in_host", "imap.evil.example"),
        ("incoming_protocol", "pop3"),
        ("out_host", "smtp.evil.example"),
        ("out_username", "other-user"),
        ("signature", "새 서명"),
        ("auto_reply_enabled", True),
    ):
        d = _outbox()
        accounts_repo.update_account(db, a1, **{field: value})
        assert _draft(db, d)["status"] == "draft", field
        assert _draft(db, d)["approved_by"] is None, field


# ---------------------------------------------------------------- 기동 복구(§7.0 "앱 기동 시")


def test_recover_on_startup_off_cancels_and_reverts_without_requeue(db: Database) -> None:
    account_id = make_account(db)
    m1 = _make_message(db, account_id, status="queued")
    m2 = _make_message(db, account_id, status="running")
    j1 = _make_job(db, m1, status="queued")
    j2 = _make_job(db, m2, status="running")
    d = _make_draft(db, account_id, status="outbox", approved_by="policy_auto_send")

    report = autoreply_repo.recover_on_startup(db, enabled=False)

    assert report.enabled is False
    assert report.cancelled_jobs == 2
    assert report.reverted_outbox == 1
    assert report.requeued_jobs == 0
    rows = _query(db, "SELECT id, status, error FROM auto_reply_jobs ORDER BY id")
    assert [(r["id"], r["status"], r["error"]) for r in rows] == [
        (j1, "cancelled", "disabled_at_startup"),
        (j2, "cancelled", "disabled_at_startup"),
    ]
    assert _draft(db, d)["status"] == "draft"
    assert _draft(db, d)["approved_by"] is None
    reasons = {r[0] for r in _query(db, "SELECT reason_code FROM auto_reply_log")}
    assert reasons == {"disabled_at_startup"}


def test_recover_on_startup_on_requeues_running_and_keeps_enable_gen(db: Database) -> None:
    account_id = make_account(db)
    autoreply_repo.enable(db, expected_generation=1, now=NOW)  # gen 2
    m = _make_message(db, account_id, status="running")
    job = _make_job(db, m, status="running", enable_gen=1)  # 옛 세대

    report = autoreply_repo.recover_on_startup(db, enabled=True)

    assert report.enabled is True
    assert report.requeued_jobs == 1
    row = _query(db, "SELECT status, enable_gen FROM auto_reply_jobs WHERE id=?", (job,))[0]
    assert row["status"] == "queued"
    assert row["enable_gen"] == 1  # 현재 세대(2)로 덮어쓰지 않는다
    assert _query(db, "SELECT auto_reply_status FROM messages WHERE id=?", (m,))[0][0] == "queued"


def test_recover_on_startup_trusts_db_over_argument(db: Database) -> None:
    """인자가 enabled=True여도 DB 유효값이 off면 off 복구를 한다(fail-closed)."""
    account_id = make_account(db)
    m = _make_message(db, account_id, status="running")
    job = _make_job(db, m, status="running")

    report = autoreply_repo.recover_on_startup(db, enabled=True)

    assert report.enabled is False
    status = _query(db, "SELECT status FROM auto_reply_jobs WHERE id=?", (job,))[0][0]
    assert status == "cancelled"


def test_preflight_counts(db: Database) -> None:
    a1 = make_account(db)
    make_account(db, email_address="b@example.com")
    _exec(db, "UPDATE accounts SET auto_reply_enabled = 1 WHERE id = ?", (a1,))
    _exec(
        db,
        "INSERT INTO rules (name, conditions_json, action, enabled, created_at, updated_at) "
        "VALUES ('r1', '[]', 'auto_send', 1, ?, ?), ('r2', '[]', 'draft', 0, ?, ?)",
        (TS, TS, TS, TS),
    )
    conn = db.new_read_connection()
    try:
        counts = autoreply_repo.preflight_counts(conn)
    finally:
        conn.close()
    assert counts == {
        "autoreply_accounts": 1,
        "rules_total": 2,
        "rules_enabled": 1,
        "rules_auto_send": 1,
        "accounts_without_trust": 1,
    }


# ---------------------------------------------------------------- 컨트롤러


def _controller(db: Database) -> tuple[AutoReplyController, list]:
    bus = EventBus()
    events: list = []
    bus.subscribe(EVENT_AUTOREPLY_ENABLED_CHANGED, lambda n, p: events.append(p))
    return AutoReplyController(db, clock=SystemClock(), event_bus=bus), events


def test_controller_enable_disable_cycle_and_events(db: Database) -> None:
    controller, events = _controller(db)

    async def _scenario() -> None:
        await controller.start_if_enabled()
        assert controller.state is ControllerState.OFF
        assert controller.active is False

        result = await controller.enable(expected_generation=1)
        assert result.enabled is True and result.generation == 2
        assert controller.state is ControllerState.ON
        assert controller.active is True
        assert controller.generation == 2

        off = await controller.disable()
        assert off.enabled is False and off.generation == 3
        assert off.drain is not None and off.drain.cancelled_jobs == 0
        assert controller.state is ControllerState.OFF
        assert controller.active is False

    asyncio.run(_scenario())
    assert events == [
        {"enabled": True, "generation": 2},
        {"enabled": False, "generation": 3},
    ]


def test_controller_enable_cas_failure_propagates_and_keeps_off(db: Database) -> None:
    controller, events = _controller(db)

    async def _scenario() -> None:
        await controller.start_if_enabled()
        with pytest.raises(PolicyError):
            await controller.enable(expected_generation=99)
        assert controller.state is ControllerState.OFF
        assert controller.active is False

    asyncio.run(_scenario())
    assert events == []
    assert _state(db).enabled is False


def test_controller_concurrent_enables_only_one_wins(db: Database) -> None:
    """같은 expected_generation으로 동시에 두 번 켜면 정확히 하나만 성공한다(R-h)."""
    controller, _events = _controller(db)

    async def _scenario() -> list:
        return await asyncio.gather(
            controller.enable(expected_generation=1),
            controller.enable(expected_generation=1),
            return_exceptions=True,
        )

    results = asyncio.run(_scenario())
    ok = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(ok) == 1 and ok[0].generation == 2
    assert len(errors) == 1 and isinstance(errors[0], PolicyError)
    assert _state(db).generation == 2


def test_controller_start_if_enabled_turns_on_from_db(db: Database) -> None:
    autoreply_repo.enable(db, expected_generation=1, now=NOW)
    controller, _ = _controller(db)

    async def _scenario() -> None:
        report = await controller.start_if_enabled()
        assert report.enabled is True
        assert controller.state is ControllerState.ON
        assert controller.generation == 2
        await controller.shutdown()
        assert controller.active is False

    asyncio.run(_scenario())
    assert _state(db).enabled is True  # shutdown은 저장값을 바꾸지 않는다


def test_controller_disable_db_failure_still_leaves_memory_off(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    autoreply_repo.enable(db, expected_generation=1, now=NOW)
    controller, _ = _controller(db)

    def _boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("db down")

    async def _scenario() -> None:
        await controller.start_if_enabled()
        assert controller.active is True
        monkeypatch.setattr(autoreply_repo, "disable_and_drain", _boom)
        with pytest.raises(RuntimeError):
            await controller.disable()
        assert controller.active is False
        assert controller.state is ControllerState.OFF

    asyncio.run(_scenario())
