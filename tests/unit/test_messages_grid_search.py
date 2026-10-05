"""`query_messages`(메일 그리드 검색) 2자 이하 LIKE 폴백 이스케이프 테스트 (QA BUG-②).

`%`/`_` 한 글자로 검색하면 `ESCAPE` 없이는 SQLite LIKE가 와일드카드로 해석해 거의
모든 메일이 "일치"해 버린다. `storage/repositories/messages.py`의
`search_message_meta`(MCP 검색 도구)는 이미 `escape_like`+`ESCAPE '!'`로 고쳐져
있었지만, 메일 그리드용 `query_messages`만 빠져 있었다.
"""

from __future__ import annotations

from pathlib import Path

from emailtomcp.storage.repositories import folders as folders_repo
from emailtomcp.storage.repositories import messages as messages_repo
from mcp_support import make_account, make_db, store_message


def _eml(message_id: str, subject: str) -> bytes:
    return (
        f"From: sender@example.com\r\n"
        f"To: receiver@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        f"Message-ID: <{message_id}@example.com>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n본문입니다.\r\n"
    ).encode()


def test_single_char_like_search_escapes_percent_and_underscore(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        folder = folders_repo.get_or_create_folder(db, account_id, "INBOX", role="inbox")
        mail_dir = tmp_path / "mail"
        mail_dir.mkdir()

        store_message(db, mail_dir, account_id, _eml("has-percent", "세일 50% 할인 안내"))
        store_message(db, mail_dir, account_id, _eml("has-underscore", "report_draft 공유"))
        store_message(db, mail_dir, account_id, _eml("plain", "그냥 평범한 메일 제목"))

        conn = db.new_read_connection()
        try:
            rows, total = messages_repo.query_messages(
                conn, folder.id, limit=50, offset=0, search_text="%"
            )
            subjects = {r.subject for r in rows}
            assert total == 1
            assert subjects == {"세일 50% 할인 안내"}

            rows, total = messages_repo.query_messages(
                conn, folder.id, limit=50, offset=0, search_text="_"
            )
            subjects = {r.subject for r in rows}
            assert total == 1
            assert subjects == {"report_draft 공유"}
        finally:
            conn.close()
    finally:
        db.stop()
