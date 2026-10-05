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


def test_migration_failure_rolls_back_user_version(tmp_path, monkeypatch) -> None:
    """마이그레이션 함수 하나가 실패하면 그 트랜잭션을 롤백하고 예외를 그대로 올린다
    (user_version은 올라가지 않는다)."""
    conn = _fresh_conn(tmp_path)
    try:

        def _boom(_conn: sqlite3.Connection) -> None:
            raise RuntimeError("boom")

        monkeypatch.setitem(runner.MIGRATIONS, 1, _boom)
        with pytest.raises(RuntimeError, match="boom"):
            runner.run_migrations(conn)
        assert runner.get_user_version(conn) == 0
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


def test_m0002_adds_parse_limited_column_defaulting_to_zero(tmp_path) -> None:
    """m0002: `messages.parse_limited`가 추가되고 기존 행은 0으로 채워진다."""
    conn = _fresh_conn(tmp_path)
    try:
        runner.run_migrations(conn)
        assert runner.CURRENT_SCHEMA_VERSION >= 2
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "parse_limited" in columns

        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        conn.execute(
            "INSERT INTO messages (account_id, folder_id, received_at, eml_path) "
            "VALUES (1, 1, ?, 'x.eml')",
            (NOW,),
        )
        conn.commit()
        row = conn.execute("SELECT parse_limited FROM messages WHERE id=1").fetchone()
        assert row[0] == 0
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


# ---------------------------------------------------------------- m0003(P3 Phase A, §7.0)


def _migrate_to(conn: sqlite3.Connection, version: int) -> None:
    """지정한 버전까지만 마이그레이션을 적용한다(이전 버전 DB 재현용)."""
    for v in range(runner.get_user_version(conn) + 1, version + 1):
        conn.execute("BEGIN")
        runner.MIGRATIONS[v](conn)
        runner.set_user_version(conn, v)
        conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _setting(conn: sqlite3.Connection, key: str):  # noqa: ANN202
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def test_m0003_adds_toggle_columns_index_and_generation(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        assert runner.run_migrations(conn) >= 3
        assert {"enable_gen", "submitted_at", "submission_sha256"} <= _columns(
            conn, "auto_reply_jobs"
        )
        assert "trusted_boundary_by" in _columns(conn, "accounts")
        assert {"recipient_norm", "thread_key"} <= _columns(conn, "auto_reply_log")
        index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_jobs_autoreply_msg'"
        ).fetchone()
        assert index is not None and "UNIQUE" in index[0].upper()
        # 세대 규칙: m0003에서 1로 시작, enabled 키는 없다(=off, I7)
        assert _setting(conn, "autoreply.generation") == "1"
        assert _setting(conn, "autoreply.enabled") is None
    finally:
        conn.close()


def test_m0003_enable_gen_defaults_to_reserved_zero_and_one_autoreply_job_per_message(
    tmp_path,
) -> None:
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
        insert_job = (
            "INSERT INTO auto_reply_jobs (kind, message_id, planned_action, status, created_at) "
            "VALUES (?, 1, 'draft', 'queued', ?)"
        )
        conn.execute(insert_job, ("auto_reply", NOW))
        assert conn.execute("SELECT enable_gen FROM auto_reply_jobs").fetchone()[0] == 0
        # 같은 메일에 auto_reply 잡 두 번째는 거부, summary 잡은 허용(부분 인덱스)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert_job, ("auto_reply", NOW))
        conn.execute(insert_job, ("summary", NOW))
        conn.execute(insert_job, ("summary", NOW))
    finally:
        conn.close()


def test_m0003_upgrade_from_v2_forces_off_and_keeps_valid_generation(tmp_path) -> None:
    """v2 시절 범용 set_setting으로 써 둔 enabled=true는 믿지 않는다(I7). 유효한 세대는 유지."""
    conn = _fresh_conn(tmp_path)
    try:
        _migrate_to(conn, 2)
        conn.execute(
            "INSERT INTO settings(key, value_json) VALUES "
            "('autoreply.enabled', 'true'), ('autoreply.generation', '5'), ('theme', '\"dark\"')"
        )
        runner.run_migrations(conn)
        assert _setting(conn, "autoreply.enabled") is None
        assert _setting(conn, "autoreply.generation") == "5"  # 세대를 줄이지 않는다
        assert _setting(conn, "theme") == '"dark"'
    finally:
        conn.close()


@pytest.mark.parametrize("raw", ['"x"', "true", "0", "-3", "{bad"])
def test_m0003_upgrade_from_v2_replaces_invalid_generation_with_one(tmp_path, raw) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        _migrate_to(conn, 2)
        conn.execute(
            "INSERT INTO settings(key, value_json) VALUES ('autoreply.generation', ?)", (raw,)
        )
        runner.run_migrations(conn)
        assert _setting(conn, "autoreply.generation") == "1"
    finally:
        conn.close()


def test_m0003_deletes_stale_v2_queue_and_autosend_state(tmp_path) -> None:
    """L-5: v2 시절 범용 경로로 쓰였을 수 있는 queue_state·autosend_state는 믿지 않고 지운다."""
    conn = _fresh_conn(tmp_path)
    try:
        _migrate_to(conn, 2)
        conn.execute(
            "INSERT INTO settings(key, value_json) VALUES "
            "('autoreply.queue_state', '\"running\"'), "
            "('autoreply.autosend_state', '\"enabled\"'), "
            "('autoreply.unmatched_action', '\"draft\"')"
        )
        runner.run_migrations(conn)
        assert _setting(conn, "autoreply.queue_state") is None
        assert _setting(conn, "autoreply.autosend_state") is None
        assert _setting(conn, "autoreply.unmatched_action") == '"draft"'  # 다른 키는 그대로
    finally:
        conn.close()


def _v2_with_preexisting_enable_gen(conn: sqlite3.Connection, enable_gen: int) -> None:
    """enable_gen 컬럼이 이미 있던 개발 DB(L-1) 재현: v2에 컬럼을 미리 넣고 잡을 만든다."""
    _migrate_to(conn, 2)
    conn.execute("ALTER TABLE auto_reply_jobs ADD COLUMN enable_gen INTEGER NOT NULL DEFAULT 0")
    conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
    conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
    conn.execute(
        "INSERT INTO messages (account_id, folder_id, received_at, eml_path) "
        "VALUES (1, 1, ?, 'x.eml')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO auto_reply_jobs (kind, message_id, planned_action, status, created_at, "
        "enable_gen) VALUES ('auto_reply', 1, 'draft', 'done', ?, ?)",
        (NOW, enable_gen),
    )


@pytest.mark.parametrize("raw", [None, '"x"', "true", "0", "-3", "{bad"])
def test_m0003_invalid_generation_uses_max_enable_gen_plus_one(tmp_path, raw) -> None:
    """L-1: 무효값은 max(1, MAX(enable_gen)+1)로 — 기존 잡의 세대와 일치할 수 없다."""
    conn = _fresh_conn(tmp_path)
    try:
        _v2_with_preexisting_enable_gen(conn, 7)
        if raw is not None:
            conn.execute(
                "INSERT INTO settings(key, value_json) VALUES ('autoreply.generation', ?)", (raw,)
            )
        runner.run_migrations(conn)
        assert _setting(conn, "autoreply.generation") == "8"
    finally:
        conn.close()


def test_m0003_valid_generation_below_max_enable_gen_is_raised(tmp_path) -> None:
    """L-1: 유효값이라도 기존 잡 세대 이하이면 MAX+1로 올린다(줄이지는 않는다)."""
    conn = _fresh_conn(tmp_path)
    try:
        _v2_with_preexisting_enable_gen(conn, 7)
        conn.execute("INSERT INTO settings(key, value_json) VALUES ('autoreply.generation', '3')")
        runner.run_migrations(conn)
        assert _setting(conn, "autoreply.generation") == "8"
    finally:
        conn.close()


def test_m0003_valid_generation_above_max_enable_gen_is_kept(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        _v2_with_preexisting_enable_gen(conn, 7)
        conn.execute("INSERT INTO settings(key, value_json) VALUES ('autoreply.generation', '20')")
        runner.run_migrations(conn)
        assert _setting(conn, "autoreply.generation") == "20"
    finally:
        conn.close()


# ---------------------------------------------------------------- m0004(데카르트 31번 M-1)


def test_m0004_marks_already_synced_folders_only(tmp_path) -> None:
    """기존 폴더: 서버에서 받은 메일(remote_uid)이 있으면 최초 동기화 완료, 없으면 0으로 둔다."""
    conn = _fresh_conn(tmp_path)
    try:
        _migrate_to(conn, 3)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'Empty', 'custom')")
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'Local', 'custom')")
        conn.execute(
            "INSERT INTO messages (account_id, folder_id, remote_uid, received_at, eml_path) "
            "VALUES (1, 1, '17', ?, 'a.eml')",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO messages (account_id, folder_id, received_at, eml_path) "
            "VALUES (1, 3, ?, 'b.eml')",
            (NOW,),
        )
        conn.commit()
        assert runner.run_migrations(conn) >= 4
        rows = dict(conn.execute("SELECT name, initial_sync_done FROM folders").fetchall())
        assert rows == {"INBOX": 1, "Empty": 0, "Local": 0}
        conn.execute("INSERT INTO folders (account_id, name) VALUES (1, 'New')")
        assert (
            conn.execute("SELECT initial_sync_done FROM folders WHERE name='New'").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_m0004_marks_inbox_by_account_trace_after_emptying(tmp_path) -> None:
    """데카르트 33번 L-7: 업그레이드 전에 받은편지함을 비워(휴지통으로 이동, remote_uid 유지)
    INBOX 자체엔 흔적이 없어도, 계정 단위 흔적으로 INBOX를 완료로 본다."""
    conn = _fresh_conn(tmp_path)
    try:
        _migrate_to(conn, 3)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'Trash', 'trash')")
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'Empty', 'custom')")
        conn.execute(
            "INSERT INTO messages (account_id, folder_id, original_folder_id, remote_uid, "
            "received_at, eml_path) VALUES (1, 2, 1, '17', ?, 'a.eml')",
            (NOW,),
        )
        conn.commit()
        runner.run_migrations(conn)
        rows = dict(conn.execute("SELECT name, initial_sync_done FROM folders").fetchall())
        assert rows == {"INBOX": 1, "Trash": 1, "Empty": 0}
    finally:
        conn.close()


def test_m0004_marks_inbox_by_original_folder_without_remote_uid(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        _migrate_to(conn, 3)
        conn.execute(_INSERT_ACCOUNT_SQL, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'Local', 'custom')")
        conn.execute(
            "INSERT INTO messages (account_id, folder_id, original_folder_id, received_at, "
            "eml_path) VALUES (1, 2, 1, ?, 'a.eml')",
            (NOW,),
        )
        conn.commit()
        runner.run_migrations(conn)
        rows = dict(conn.execute("SELECT name, initial_sync_done FROM folders").fetchall())
        assert rows == {"INBOX": 1, "Local": 0}
    finally:
        conn.close()


def test_m0004_marks_pop3_inbox_with_seen_uidls(tmp_path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        _migrate_to(conn, 3)
        pop3_sql = _INSERT_ACCOUNT_SQL.replace("'imap','imap.example.com'", "'pop3','pop'")
        conn.execute(pop3_sql, (NOW, NOW))
        conn.execute("INSERT INTO folders (account_id, name, role) VALUES (1, 'INBOX', 'inbox')")
        conn.execute(
            "INSERT INTO pop3_uidl_seen (account_id, uidl, first_seen_at) VALUES (1, 'u1', ?)",
            (NOW,),
        )
        conn.commit()
        runner.run_migrations(conn)
        assert conn.execute("SELECT initial_sync_done FROM folders").fetchone()[0] == 1
    finally:
        conn.close()
