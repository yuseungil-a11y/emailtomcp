from __future__ import annotations

import sqlite3

import pytest

from emailtomcp.core.errors import PermanentError
from emailtomcp.storage.migrations import runner

NOW = "2026-01-01T00:00:00Z"

_INSERT_ACCOUNT_SQL = (
    "INSERT INTO accounts (display_name, email_address, incoming_protocol, in_host, in_port, "
    "in_security, in_username, out_host, out_port, out_security, created_at, updated_at) "
    "VALUES ('acc','acc@example.com','imap','imap.example.com',993,'ssl','u',"
    "'smtp.example.com',465,'ssl',?,?)"
)


def _fresh_conn(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "test.sqlite3")
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_fresh_db_migrates_to_current_version(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        version = runner.run_migrations(conn)
        assert version == runner.CURRENT_SCHEMA_VERSION

        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        expected = {
            "accounts",
            "folders",
            "messages",
            "attachments",
            "drafts",
            "draft_recipients",
            "rules",
            "auto_reply_jobs",
            "auto_reply_log",
            "pop3_uidl_seen",
            "mcp_tokens",
            "mcp_audit",
            "settings",
        }
        assert expected <= tables
        # DESIGN.md v0.2 §5.1 "삭제" 항목: 아래 테이블/컬럼은 MVP에서 두지 않는다.
        assert "send_log" not in tables  # S-12
        assert "prompt_templates" not in tables  # S-16
    finally:
        conn.close()


def test_messages_has_no_is_deleted_column(tmp_path) -> None:
    """C-08: 삭제는 휴지통 폴더 이동 하나로 통일한다 — is_deleted 컬럼은 두지 않는다."""
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "is_deleted" not in columns
        assert "original_folder_id" in columns  # 휴지통 복구용
    finally:
        conn.close()


def test_rerunning_migrations_is_noop(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        version_again = runner.run_migrations(conn)
        assert version_again == runner.CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_future_user_version_is_rejected(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        conn.execute(f"PRAGMA user_version = {runner.CURRENT_SCHEMA_VERSION + 1}")
        with pytest.raises(PermanentError):
            runner.run_migrations(conn)
    finally:
        conn.close()


def test_auto_reply_status_check_constraint_rejects_bogus_value(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages (account_id, folder_id, received_at, eml_path, "
                "auto_reply_status) VALUES (1, 1, ?, 'x.eml', 'bogus')",
                (NOW,),
            )
    finally:
        conn.close()


def test_auto_reply_status_allows_null(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        conn.execute(
            "INSERT INTO messages (account_id, folder_id, received_at, eml_path) "
            "VALUES (1, 1, ?, 'x.eml')",
            (NOW,),
        )
        conn.commit()
        row = conn.execute("SELECT auto_reply_status FROM messages WHERE id=1").fetchone()
        assert row[0] is None
    finally:
        conn.close()


def test_auto_reply_status_accepts_all_designed_values(tmp_path) -> None:
    """§5.2: ignored/blocked/queued/running/sent/drafted/skipped/failed/cancelled 전부 허용."""
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        values = (
            "ignored",
            "blocked",
            "queued",
            "running",
            "sent",
            "drafted",
            "skipped",
            "failed",
            "cancelled",
        )
        for i, value in enumerate(values):
            conn.execute(
                "INSERT INTO messages (account_id, folder_id, received_at, eml_path, "
                "auto_reply_status) VALUES (1, 1, ?, ?, ?)",
                (NOW, f"x{i}.eml", value),
            )
        conn.commit()
    finally:
        conn.close()


def test_fts_trigger_syncs_on_insert(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        conn.execute(
            "INSERT INTO messages (account_id, folder_id, received_at, eml_path, subject, "
            "body_text) VALUES (1, 1, ?, 'x.eml', '테스트제목', '본문내용')",
            (NOW,),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH '테스트제목'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_fts_trigger_syncs_on_delete(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        conn.execute(
            "INSERT INTO messages (account_id, folder_id, received_at, eml_path, subject) "
            "VALUES (1, 1, ?, 'x.eml', '삭제될메일')",
            (NOW,),
        )
        conn.commit()
        conn.execute("DELETE FROM messages WHERE id=1")
        conn.commit()

        rows = conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH '삭제될메일'"
        ).fetchall()
        assert rows == []
    finally:
        conn.close()


def test_fts_trigger_syncs_on_update(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        conn.execute(
            "INSERT INTO messages (account_id, folder_id, received_at, eml_path, subject) "
            "VALUES (1, 1, ?, 'x.eml', '원래제목')",
            (NOW,),
        )
        conn.commit()
        conn.execute("UPDATE messages SET subject='바뀐제목' WHERE id=1")
        conn.commit()

        assert (
            conn.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH '원래제목'"
            ).fetchall()
            == []
        )
        rows = conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH '바뀐제목'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_drafts_kind_check_accepts_forward_inline_and_attach(tmp_path) -> None:
    """DraftKind.FORWARD_INLINE/FORWARD_ATTACH (v0.2 §5.1) — 'forward' 단일값은 폐기됐다."""
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        for kind in ("forward_inline", "forward_attach"):
            conn.execute(
                "INSERT INTO drafts (account_id, kind, origin, created_at, updated_at) "
                "VALUES (1, ?, 'user', ?, ?)",
                (kind, NOW, NOW),
            )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO drafts (account_id, kind, origin, created_at, updated_at) "
                "VALUES (1, 'forward', 'user', ?, ?)",
                (NOW, NOW),
            )
    finally:
        conn.close()


def test_drafts_status_check_accepts_outbox_and_send_unknown(tmp_path) -> None:
    """C-19 Outbox/재발송: outbox, send_unknown 상태가 새로 추가됐다."""
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        for status in ("outbox", "send_unknown"):
            conn.execute(
                "INSERT INTO drafts (account_id, kind, origin, status, created_at, updated_at) "
                "VALUES (1, 'new', 'user', ?, ?, ?)",
                (status, NOW, NOW),
            )
        conn.commit()
    finally:
        conn.close()
