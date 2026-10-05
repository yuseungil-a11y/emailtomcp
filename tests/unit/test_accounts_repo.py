"""`accounts` 레포지토리 단위 테스트 (DESIGN.md §5.1)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from emailtomcp.storage.repositories import accounts as accounts_repo
from mcp_support import make_account, make_db

pytestmark = pytest.mark.unit


def test_effective_out_username_falls_back_when_same_as_in() -> None:
    conn_account = accounts_repo.Account(
        id=1,
        display_name="d",
        email_address="e@x.example",
        aliases=[],
        sender_name=None,
        provider_preset=None,
        auth_method="password",
        incoming_protocol="imap",
        in_host="imap.x",
        in_port=993,
        in_security="ssl",
        in_username="in-user",
        out_host="smtp.x",
        out_port=465,
        out_security="ssl",
        out_username=None,
        out_auth_same_as_in=True,
        pop3_leave_on_server=True,
        pop3_delete_after_days=None,
        poll_interval_sec=300,
        signature=None,
        trusted_authserv_id=None,
        auto_reply_enabled=False,
        enabled=True,
    )
    assert conn_account.effective_out_username == "in-user"

    distinct = dataclasses.replace(
        conn_account, out_auth_same_as_in=False, out_username="out-user"
    )
    assert distinct.effective_out_username == "out-user"

    # out_auth_same_as_in=False인데 out_username이 비어 있으면 그래도 in_username을 쓴다.
    empty_out = dataclasses.replace(conn_account, out_auth_same_as_in=False, out_username="")
    assert empty_out.effective_out_username == "in-user"


def test_insert_account_rolls_back_and_raises_on_check_violation(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        with pytest.raises(Exception, match="CHECK"):
            accounts_repo.insert_account(
                db,
                display_name="d",
                email_address="e@x.example",
                incoming_protocol="bogus-protocol",  # CHECK(incoming_protocol IN ('pop3','imap'))
                in_host="imap.x",
                in_port=993,
                in_security="ssl",
                in_username="u",
                out_host="smtp.x",
                out_port=465,
                out_security="ssl",
            )
        conn = db.new_read_connection()
        try:
            assert accounts_repo.list_accounts(conn) == []
        finally:
            conn.close()
    finally:
        db.stop()


def test_update_account_with_no_updatable_fields_returns_false(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        assert accounts_repo.update_account(db, account_id, not_a_real_column="x") is False
    finally:
        db.stop()


def test_update_account_persists_allowed_fields_and_rejects_unknown(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        ok = accounts_repo.update_account(
            db, account_id, display_name="새 이름", password="무시됨(허용 컬럼 아님)"
        )
        assert ok is True
        conn = db.new_read_connection()
        try:
            account = accounts_repo.get_account(conn, account_id)
        finally:
            conn.close()
        assert account.display_name == "새 이름"
    finally:
        db.stop()


def test_update_account_unknown_account_id_returns_false(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        assert accounts_repo.update_account(db, 424242, display_name="x") is False
    finally:
        db.stop()


def test_set_aliases_persists_json(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        accounts_repo.set_aliases(db, account_id, ["a@x.example", "b@x.example"])
        conn = db.new_read_connection()
        try:
            account = accounts_repo.get_account(conn, account_id)
        finally:
            conn.close()
        assert account.aliases == ["a@x.example", "b@x.example"]
    finally:
        db.stop()


def test_list_enabled_accounts_excludes_disabled(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        enabled_id = make_account(db, "on@example.com")
        disabled_id = make_account(db, "off@example.com")
        accounts_repo.update_account(db, disabled_id, enabled=False)
        conn = db.new_read_connection()
        try:
            ids = {a.id for a in accounts_repo.list_enabled_accounts(conn)}
        finally:
            conn.close()
        assert enabled_id in ids and disabled_id not in ids
    finally:
        db.stop()
