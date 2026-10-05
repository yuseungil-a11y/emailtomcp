"""`pop3_uidl_seen` 레포지토리 단위 테스트 (DESIGN.md §2(b))."""

from __future__ import annotations

from pathlib import Path

import pytest

from emailtomcp.storage.repositories import pop3_uidl
from mcp_support import make_account, make_db

pytestmark = pytest.mark.unit


def test_record_seen_is_idempotent_and_orders_by_first_seen(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        pop3_uidl.record_seen(db, account_id, "u2", "2026-01-02T00:00:00+00:00")
        pop3_uidl.record_seen(db, account_id, "u1", "2026-01-01T00:00:00+00:00")
        # 같은 UIDL을 다시 기록해도(INSERT OR IGNORE) 중복되지 않는다.
        pop3_uidl.record_seen(db, account_id, "u1", "2099-01-01T00:00:00+00:00")

        conn = db.new_read_connection()
        try:
            seen = pop3_uidl.get_seen_uidls(conn, account_id)
            ordered = pop3_uidl.list_seen(conn, account_id)
        finally:
            conn.close()
        assert seen == {"u1", "u2"}
        assert [row.uidl for row in ordered] == ["u1", "u2"]  # first_seen_at 순
        assert ordered[0].first_seen_at == "2026-01-01T00:00:00+00:00"  # 재기록은 무시됨
    finally:
        db.stop()


def test_record_seen_rolls_back_and_raises_on_fk_violation(tmp_path: Path) -> None:
    """존재하지 않는 account_id는 외래키 위반이다 — `INSERT OR IGNORE`도 FK 위반은 무시하지
    않으므로(SQLite 명세), 커밋되지 않고 롤백한 뒤 예외를 그대로 올린다."""
    db = make_db(tmp_path)
    try:
        with pytest.raises(Exception, match="FOREIGN KEY"):
            pop3_uidl.record_seen(db, 424242, "u1", "2026-01-01T00:00:00+00:00")
    finally:
        db.stop()


def test_get_seen_uidls_empty_for_unknown_account(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        conn = db.new_read_connection()
        try:
            assert pop3_uidl.get_seen_uidls(conn, account_id) == set()
            assert pop3_uidl.list_seen(conn, account_id) == []
        finally:
            conn.close()
    finally:
        db.stop()
