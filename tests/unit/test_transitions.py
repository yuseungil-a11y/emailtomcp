from __future__ import annotations

import sqlite3

import pytest

from emailtomcp.storage.migrations import runner
from emailtomcp.storage.transitions import transition

NOW = "2026-01-01T00:00:00Z"


def _conn(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "t.sqlite3")
    conn.isolation_level = None
    runner.run_migrations(conn)
    return conn


def _insert_job(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO accounts (display_name, email_address, incoming_protocol, in_host, in_port, "
        "in_security, in_username, out_host, out_port, out_security, created_at, updated_at) "
        "VALUES ('a','a@example.com','imap','h',993,'ssl','u','h',465,'ssl',?,?)",
        (NOW, NOW),
    )
    conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
    conn.execute(
        "INSERT INTO messages (account_id, folder_id, received_at, eml_path) "
        "VALUES (1, 1, ?, 'x.eml')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO auto_reply_jobs (kind, message_id, planned_action, status, created_at) "
        "VALUES ('auto_reply', 1, 'auto_send', 'queued', ?)",
        (NOW,),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_transition_success(tmp_path) -> None:
    conn = _conn(tmp_path)
    job_id = _insert_job(conn)

    ok = transition(conn, "auto_reply_jobs", job_id, ["queued"], "running")

    assert ok is True
    row = conn.execute("SELECT status FROM auto_reply_jobs WHERE id=?", (job_id,)).fetchone()
    assert row[0] == "running"


def test_transition_fails_when_not_in_allowed_from_state(tmp_path) -> None:
    conn = _conn(tmp_path)
    job_id = _insert_job(conn)
    transition(conn, "auto_reply_jobs", job_id, ["queued"], "running")

    # 이미 running이므로 'queued' -> 'done' 전이는 실패해야 한다(경쟁 상태 방지).
    ok = transition(conn, "auto_reply_jobs", job_id, ["queued"], "done")

    assert ok is False
    row = conn.execute("SELECT status FROM auto_reply_jobs WHERE id=?", (job_id,)).fetchone()
    assert row[0] == "running"


def test_transition_on_messages_uses_auto_reply_status_column(tmp_path) -> None:
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO accounts (display_name, email_address, incoming_protocol, in_host, in_port, "
        "in_security, in_username, out_host, out_port, out_security, created_at, updated_at) "
        "VALUES ('a','a@example.com','imap','h',993,'ssl','u','h',465,'ssl',?,?)",
        (NOW, NOW),
    )
    conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
    conn.execute(
        "INSERT INTO messages (account_id, folder_id, received_at, eml_path, auto_reply_status) "
        "VALUES (1, 1, ?, 'x.eml', 'queued')",
        (NOW,),
    )
    conn.commit()
    message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    ok = transition(conn, "messages", message_id, ["queued"], "sent")

    assert ok is True
    row = conn.execute(
        "SELECT auto_reply_status FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    assert row[0] == "sent"


def test_transition_rejects_unknown_table() -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError):
        transition(conn, "not_a_real_table", 1, ["a"], "b")


def test_transition_rejects_empty_from_states(tmp_path) -> None:
    conn = _conn(tmp_path)
    job_id = _insert_job(conn)
    with pytest.raises(ValueError):
        transition(conn, "auto_reply_jobs", job_id, [], "running")
