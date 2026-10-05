"""mail 서비스 단위 테스트 공용 헬퍼. `test_*.py`가 아니므로 pytest가 수집하지 않는다."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from emailtomcp.mail.mime.parse import parse_eml
from emailtomcp.mail.sync_service import SyncService
from emailtomcp.storage.db import Database
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import folders as folders_repo
from emailtomcp.storage.repositories import messages as messages_repo


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / f"test-{uuid.uuid4().hex}.sqlite3")
    db.start()
    db.migrate()
    return db


def make_account(db: Database, **overrides: object) -> int:
    defaults: dict[str, object] = {
        "display_name": "테스트계정",
        "email_address": "me@example.com",
        "incoming_protocol": "imap",
        "in_host": "imap.example.com",
        "in_port": 993,
        "in_security": "ssl",
        "in_username": "me@example.com",
        "out_host": "smtp.example.com",
        "out_port": 465,
        "out_security": "ssl",
    }
    defaults.update(overrides)
    return accounts_repo.insert_account(db, **defaults)  # type: ignore[arg-type]


def store_message(
    db: Database,
    mail_dir: Path,
    account_id: int,
    raw: bytes,
    *,
    folder_name: str = "INBOX",
    folder_role: str | None = "inbox",
) -> int:
    """fixture `.eml`을 디스크에 쓰고 파싱 결과를 메시지로 저장한다(sync_service 재사용)."""
    folder = folders_repo.get_or_create_folder(db, account_id, folder_name, role=folder_role)
    eml_path = mail_dir / f"{uuid.uuid4()}.eml"
    eml_path.write_bytes(raw)
    parsed = parse_eml(raw)
    data = SyncService.to_message_insert(
        account_id=account_id,
        folder_id=folder.id,
        parsed=parsed,
        remote_uid=None,
        eml_path=str(eml_path),
        received_at=datetime.now(UTC),
        is_read=False,
        is_flagged=False,
    )
    return messages_repo.insert_message(db, data)
