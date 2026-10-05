"""`DraftService` 테스트 (DESIGN.md §1.2 #23 전체회신 제외/첨부 기본 포함)."""

from __future__ import annotations

from pathlib import Path

from _helpers import make_account, make_db, store_message

from emailtomcp.mail.draft_service import DraftService, compute_snapshot_hash

ORIGINAL_EML = (
    b"From: =?UTF-8?B?6rCV7LKY7IiY?= <kim@example.com>\r\n"
    b"To: =?UTF-8?B?64uI?= <me@example.com>, =?UTF-8?B?67aA7JWE7Zmd?= <park@example.com>\r\n"
    b"Cc: =?UTF-8?B?7J2066+47IiY?= <lee@example.com>\r\n"
    b"Subject: =?UTF-8?B?7Zig7JuQIOyViOuCtA==?=\r\n"
    b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
    b"Message-ID: <orig-reply-001@example.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="B1"\r\n'
    b"\r\n"
    b"--B1\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n"
    b"\r\n"
    b"\xec\x9b\x90\xeb\xb0\x94\x20\xeb\xb3\xb8\xeb\xac\xb8\xec\x9e\x85\xeb\x8b\x88\xeb\x8b\xa4.\r\n"
    b"\r\n"
    b"--B1\r\n"
    b"Content-Type: application/pdf\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b'Content-Disposition: attachment; filename="doc.pdf"\r\n'
    b"\r\n"
    b"JVBERi0xLjQ=\r\n"
    b"\r\n"
    b"--B1--\r\n"
)


def _setup(tmp_path: Path):
    db = make_db(tmp_path)
    mail_dir = tmp_path / "mail"
    mail_dir.mkdir()
    account_id = make_account(
        db,
        email_address="me@example.com",
        aliases=["alias@example.com"],
    )
    source_message_id = store_message(db, mail_dir, account_id, ORIGINAL_EML)
    return db, account_id, source_message_id


def test_create_reply_draft_targets_sender_and_includes_attachment_by_default(
    tmp_path: Path,
) -> None:
    db, account_id, source_message_id = _setup(tmp_path)
    try:
        service = DraftService(db)
        draft_id = service.create_reply_draft(
            account_id=account_id,
            source_message_id=source_message_id,
            body_text="회신 본문입니다",
        )
        conn = db.new_read_connection()
        try:
            from emailtomcp.storage.repositories import drafts as drafts_repo

            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()

        assert draft is not None
        assert draft.kind == "reply"
        assert draft.to_addrs == ["kim@example.com"]
        assert draft.cc_addrs == []
        assert "회신 본문입니다" in (draft.body_text or "")
        assert len(draft.attachments) == 1
        assert draft.attachments[0]["source"] == "original_part"
    finally:
        db.stop()


def test_create_reply_all_draft_excludes_self_and_aliases(tmp_path: Path) -> None:
    db, account_id, source_message_id = _setup(tmp_path)
    try:
        service = DraftService(db)
        draft_id = service.create_reply_draft(
            account_id=account_id,
            source_message_id=source_message_id,
            body_text="전체 회신 본문",
            reply_all=True,
        )
        conn = db.new_read_connection()
        try:
            from emailtomcp.storage.repositories import drafts as drafts_repo

            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()

        assert draft is not None
        assert draft.kind == "reply_all"
        to_set = set(draft.to_addrs)
        assert "me@example.com" not in to_set
        assert "kim@example.com" in to_set
        assert "park@example.com" in to_set
        assert draft.cc_addrs == ["lee@example.com"]
    finally:
        db.stop()


def test_create_reply_draft_can_exclude_attachments(tmp_path: Path) -> None:
    db, account_id, source_message_id = _setup(tmp_path)
    try:
        service = DraftService(db)
        draft_id = service.create_reply_draft(
            account_id=account_id,
            source_message_id=source_message_id,
            body_text="첨부 없이",
            include_attachments=False,
        )
        conn = db.new_read_connection()
        try:
            from emailtomcp.storage.repositories import drafts as drafts_repo

            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.attachments == []
    finally:
        db.stop()


def test_create_forward_inline_draft(tmp_path: Path) -> None:
    db, account_id, source_message_id = _setup(tmp_path)
    try:
        service = DraftService(db)
        draft_id = service.create_forward_draft(
            account_id=account_id,
            source_message_id=source_message_id,
            to_addrs=["dest@example.com"],
            body_text="전달합니다",
            mode="inline",
        )
        conn = db.new_read_connection()
        try:
            from emailtomcp.storage.repositories import drafts as drafts_repo

            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.kind == "forward_inline"
        assert "원바 본문입니다" in (draft.body_text or "") or "본문입니다" in (
            draft.body_text or ""
        )
        assert draft.subject is not None and draft.subject.lower().startswith("fwd")
    finally:
        db.stop()


def test_create_forward_attach_draft(tmp_path: Path) -> None:
    db, account_id, source_message_id = _setup(tmp_path)
    try:
        service = DraftService(db)
        draft_id = service.create_forward_draft(
            account_id=account_id,
            source_message_id=source_message_id,
            to_addrs=["dest@example.com"],
            body_text="첨부로 전달",
            mode="attach",
        )
        conn = db.new_read_connection()
        try:
            from emailtomcp.storage.repositories import drafts as drafts_repo

            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.kind == "forward_attach"
        assert draft.attachments[0]["source"] == "original_eml"
    finally:
        db.stop()


def test_stage_local_attachment_copies_into_mail_blob_dir(tmp_path: Path) -> None:
    db, account_id, source_message_id = _setup(tmp_path)
    del account_id, source_message_id
    try:
        mail_dir = tmp_path / "mail"
        source_file = tmp_path / "upload.txt"
        source_file.write_bytes(b"hello attachment")
        service = DraftService(db, mail_blob_dir=mail_dir)

        meta = service.stage_local_attachment(str(source_file))

        assert meta["source"] == "local_file"
        assert meta["filename"] == "upload.txt"
        assert meta["size_bytes"] == len(b"hello attachment")
        staged_path = Path(meta["path"])
        assert staged_path.is_file()
        assert staged_path.read_bytes() == b"hello attachment"
    finally:
        db.stop()


def test_update_compose_only_affects_draft_status(tmp_path: Path) -> None:
    db, account_id, _source_message_id = _setup(tmp_path)
    try:
        service = DraftService(db)
        draft_id = service.create_draft(
            account_id=account_id,
            to_addrs=["dest@example.com"],
            subject="원래 제목",
            body_text="원래 본문",
        )

        changed = service.update_compose(
            draft_id, to_addrs=["new@example.com"], subject="새 제목", body_text="새 본문"
        )
        assert changed is True

        conn = db.new_read_connection()
        try:
            from emailtomcp.storage.repositories import drafts as drafts_repo

            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.subject == "새 제목"
        assert draft.to_addrs == ["new@example.com"]
    finally:
        db.stop()


def test_discard_draft_removes_draft_status_only(tmp_path: Path) -> None:
    db, account_id, _source_message_id = _setup(tmp_path)
    try:
        service = DraftService(db)
        draft_id = service.create_draft(
            account_id=account_id, to_addrs=["dest@example.com"], subject="제목", body_text="본문"
        )
        assert service.discard_draft(draft_id) is True

        conn = db.new_read_connection()
        try:
            from emailtomcp.storage.repositories import drafts as drafts_repo

            assert drafts_repo.get_draft(conn, draft_id) is None
        finally:
            conn.close()
    finally:
        db.stop()


def test_compute_snapshot_hash_is_deterministic() -> None:
    h1 = compute_snapshot_hash(
        to_addrs=["b@example.com", "a@example.com"],
        cc_addrs=[],
        bcc_addrs=[],
        subject="제목",
        body_text="본문",
        attachments=[],
    )
    h2 = compute_snapshot_hash(
        to_addrs=["a@example.com", "b@example.com"],  # 순서만 다름
        cc_addrs=[],
        bcc_addrs=[],
        subject="제목",
        body_text="본문",
        attachments=[],
    )
    assert h1 == h2  # 정규화 후 비교라 순서 무관

    h3 = compute_snapshot_hash(
        to_addrs=["a@example.com", "b@example.com"],
        cc_addrs=[],
        bcc_addrs=[],
        subject="다른 제목",
        body_text="본문",
        attachments=[],
    )
    assert h1 != h3
