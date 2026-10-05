"""송수신 이력 기반 주소 자동완성 조회 단위 테스트 (DESIGN.md §1.2 #11, §11.2).

받은 메일 발신자(messages.from_addr)와 보낸 메일 수신자(drafts.to/cc/bcc, status='sent')를
합쳐 최근 주고받은 주소 후보를 만드는 `list_known_addresses`를 확인한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emailtomcp.mail.draft_service import DraftService
from emailtomcp.storage.repositories import address_history
from mcp_support import KOREAN_EML, make_account, make_db, store_message

pytestmark = pytest.mark.unit


def _mark_sent(db: object, draft_id: int) -> None:
    def _write(conn: object) -> None:
        conn.execute(  # type: ignore[attr-defined]
            "UPDATE drafts SET status = 'sent', sent_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (draft_id,),
        )
        conn.commit()  # type: ignore[attr-defined]

    db.writer.submit(_write).result()  # type: ignore[attr-defined]


def test_list_known_addresses_merges_received_senders_and_sent_recipients(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        mail_dir = tmp_path / "mail"
        mail_dir.mkdir()
        store_message(db, mail_dir, account_id, KOREAN_EML)  # From: kim@partner.example

        draft_id = DraftService(db, mail_blob_dir=mail_dir).create_draft(
            account_id=account_id,
            to_addrs=["a@x.example"],
            cc_addrs=["b@x.example"],
            subject="s",
            body_text="b",
        )
        _mark_sent(db, draft_id)

        conn = db.new_read_connection()
        try:
            addrs = address_history.list_known_addresses(conn, account_id)
        finally:
            conn.close()
        assert "kim@partner.example" in addrs
        assert "a@x.example" in addrs and "b@x.example" in addrs
    finally:
        db.stop()


def test_list_known_addresses_ignores_malformed_json(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        mail_dir = tmp_path / "mail"
        mail_dir.mkdir()
        service = DraftService(db, mail_blob_dir=mail_dir)
        good_id = service.create_draft(
            account_id=account_id, to_addrs=["ok@x.example"], subject="s", body_text="b"
        )
        _mark_sent(db, good_id)
        bad_id = service.create_draft(
            account_id=account_id, to_addrs=["also-ok@x.example"], subject="s2", body_text="b"
        )
        _mark_sent(db, bad_id)

        def _corrupt(conn: object) -> None:
            conn.execute("UPDATE drafts SET to_addrs = 'not-json' WHERE id = ?", (bad_id,))  # type: ignore[attr-defined]
            conn.commit()  # type: ignore[attr-defined]

        db.writer.submit(_corrupt).result()

        conn = db.new_read_connection()
        try:
            # 깨진 JSON(바로 위 _corrupt)이 섞여 있어도 예외 없이 무시되고 나머지는 반환된다.
            addrs = address_history.list_known_addresses(conn, account_id)
        finally:
            conn.close()
        assert "ok@x.example" in addrs
        assert "not-json" not in addrs
    finally:
        db.stop()


def test_list_known_addresses_respects_limit(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        mail_dir = tmp_path / "mail"
        mail_dir.mkdir()
        service = DraftService(db, mail_blob_dir=mail_dir)
        for i in range(3):
            draft_id = service.create_draft(
                account_id=account_id, to_addrs=[f"n{i}@x.example"], subject="s", body_text="b"
            )
            _mark_sent(db, draft_id)

        conn = db.new_read_connection()
        try:
            addrs = address_history.list_known_addresses(conn, account_id, limit=1)
        finally:
            conn.close()
        assert len(addrs) == 1
    finally:
        db.stop()


def test_list_known_addresses_empty_account_returns_empty_list(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        conn = db.new_read_connection()
        try:
            assert address_history.list_known_addresses(conn, account_id) == []
        finally:
            conn.close()
    finally:
        db.stop()
