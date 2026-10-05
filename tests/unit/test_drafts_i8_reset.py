"""I8 / M-B: approved_by 초기화 규칙 (DESIGN.md §5.3, §7.9).

`approved_by='policy_auto_send'`인 초안이 **어떤 경로로든** draft로 돌아가면 단일 전이 함수
(`storage.transitions.transition`)가 approved_by·approved_at·outbox_expires_at을 NULL로 만든다.
다른 approved_by 값(`user_send`, `ui`, `policy_allowlist`)의 기존 동작(P2)은 바뀌지 않아야 한다.

Phase A 시점에는 policy_auto_send 행을 만드는 코드(G7)가 없으므로 테스트에서 직접 만든다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers import make_account, make_db

from emailtomcp.storage.db import Database
from emailtomcp.storage.repositories import drafts as drafts_repo
from emailtomcp.storage.transitions import transition

pytestmark = pytest.mark.unit

TS = "2026-10-05T05:00:00+00:00"


@pytest.fixture
def db(tmp_path: Path):  # noqa: ANN201
    database = make_db(tmp_path)
    yield database
    database.stop()


def _insert(
    db: Database, *, status: str, approved_by: str | None, origin: str = "autoreply"
) -> int:
    account_id = make_account(db)

    def _w(conn):  # noqa: ANN001, ANN202
        conn.execute("BEGIN")
        cur = conn.execute(
            "INSERT INTO drafts (account_id, kind, origin, status, approved_by, approved_at, "
            "outbox_expires_at, message_id_hdr, created_at, updated_at) "
            "VALUES (?, 'reply', ?, ?, ?, ?, ?, '<m@x>', ?, ?)",
            (
                account_id,
                origin,
                status,
                approved_by,
                TS if approved_by else None,
                TS if approved_by else None,
                TS,
                TS,
            ),
        )
        conn.commit()
        return cur.lastrowid

    return db.writer.submit(_w).result()


def _row(db: Database, draft_id: int):  # noqa: ANN202
    conn = db.new_read_connection()
    try:
        return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    finally:
        conn.close()


def _assert_reset(row) -> None:  # noqa: ANN001
    assert row["status"] == "draft"
    assert row["approved_by"] is None
    assert row["approved_at"] is None
    assert row["outbox_expires_at"] is None


# ---------------------------------------------------------------- policy_auto_send → 초기화


def test_expired_autosend_outbox_to_draft_resets_approval(db: Database) -> None:
    draft_id = _insert(db, status="outbox", approved_by="policy_auto_send")
    assert drafts_repo.revert_expired_outbox_to_draft(db, draft_id) is True
    _assert_reset(_row(db, draft_id))


@pytest.mark.parametrize("from_status", ["outbox", "failed", "send_unknown"])
def test_every_draft_transition_resets_policy_auto_send(db: Database, from_status: str) -> None:
    draft_id = _insert(db, status=from_status, approved_by="policy_auto_send")
    assert drafts_repo.transition_status(db, draft_id, [from_status], "draft") is True
    _assert_reset(_row(db, draft_id))


def test_non_draft_target_keeps_policy_auto_send(db: Database) -> None:
    """초기화는 to_state='draft'일 때만 — outbox→sending 같은 전이는 승인 정보를 유지한다."""
    draft_id = _insert(db, status="outbox", approved_by="policy_auto_send")
    assert drafts_repo.mark_sending(db, draft_id) is True
    row = _row(db, draft_id)
    assert row["status"] == "sending"
    assert row["approved_by"] == "policy_auto_send"
    assert row["approved_at"] == TS
    assert row["outbox_expires_at"] == TS


# ---------------------------------------------------------------- 기존(P2) 동작 유지


@pytest.mark.parametrize(
    ("approved_by", "origin"),
    [("user_send", "user"), ("ui", "mcp"), ("policy_allowlist", "mcp")],
)
def test_other_approvers_are_unchanged_on_draft_transition(
    db: Database, approved_by: str, origin: str
) -> None:
    draft_id = _insert(db, status="outbox", approved_by=approved_by, origin=origin)
    assert drafts_repo.revert_expired_outbox_to_draft(db, draft_id) is True
    row = _row(db, draft_id)
    assert row["status"] == "draft"
    assert row["approved_by"] == approved_by  # P2 MCP 레이트리밋 집계 근거 유지
    assert row["approved_at"] == TS
    assert row["outbox_expires_at"] == TS


def test_pending_approval_revert_unchanged(db: Database) -> None:
    draft_id = _insert(db, status="pending_approval", approved_by=None, origin="mcp")
    assert drafts_repo.revert_approval_to_draft(db, draft_id) is True
    row = _row(db, draft_id)
    assert row["status"] == "draft"
    assert row["approved_by"] is None
    assert row["origin"] == "mcp"


def test_transition_contention_still_returns_false(db: Database) -> None:
    draft_id = _insert(db, status="sending", approved_by="policy_auto_send")
    assert drafts_repo.revert_expired_outbox_to_draft(db, draft_id) is False
    row = _row(db, draft_id)
    assert row["status"] == "sending"
    assert row["approved_by"] == "policy_auto_send"


# ---------------------------------------------------------------- commit=False(트랜잭션 묶기)


def test_transition_commit_false_participates_in_caller_transaction(db: Database) -> None:
    draft_id = _insert(db, status="outbox", approved_by="policy_auto_send")

    def _w(conn):  # noqa: ANN001, ANN202
        conn.execute("BEGIN")
        ok = transition(conn, "drafts", draft_id, ["outbox"], "draft", commit=False)
        assert conn.in_transaction  # 커밋하지 않았다
        conn.rollback()
        return ok

    assert db.writer.submit(_w).result() is True
    row = _row(db, draft_id)
    assert row["status"] == "outbox"  # 롤백되어 원래대로
    assert row["approved_by"] == "policy_auto_send"
