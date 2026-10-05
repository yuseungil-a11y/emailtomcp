"""`UiApi.move_to_trash`/`restore_from_trash`의 IMAP 서버 반영 테스트.

DESIGN.md §2(b)(N-01 보정)와 §12.3 P1 시나리오 ⑯("IMAP 이동/휴지통/복구가 서버에
반영됨")을 실제로 검증한다. 데카르트의 P1 기능검증(`docs/reviews/06_QA_P1기능검증_제우스.md`)
에서 발견된 Go 차단 항목(로컬 DB만 바뀌고 서버에는 반영 안 됨)을 고친 뒤 추가했다.

이 레포의 async 백엔드 메서드는 pytest-asyncio 없이 `asyncio.run(...)`으로 돌리는
방식을 쓴다(다른 unit 테스트들과 동일한 관례).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
from fake_imap_server import FakeImapServer  # noqa: E402

from emailtomcp.app import UiApi  # noqa: E402
from emailtomcp.core.clock import SystemClock  # noqa: E402
from emailtomcp.mail.mime.parse import parse_eml  # noqa: E402
from emailtomcp.mail.sync_service import SyncService  # noqa: E402
from emailtomcp.secrets import keyring_store  # noqa: E402
from emailtomcp.secrets.keyring_store import KeyringSecretStore, set_account_password  # noqa: E402
from emailtomcp.storage.db import Database  # noqa: E402
from emailtomcp.storage.repositories import accounts as accounts_repo  # noqa: E402
from emailtomcp.storage.repositories import folders as folders_repo  # noqa: E402
from emailtomcp.storage.repositories import messages as messages_repo  # noqa: E402

pytestmark = pytest.mark.integration

SIMPLE_EML = (
    b"From: sender@example.com\r\nTo: me@example.com\r\nSubject: hi\r\n"
    b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\nMessage-ID: <uiapi-move-001@example.com>\r\n"
    b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
)


def _make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / f"test-{uuid.uuid4().hex}.sqlite3")
    db.start()
    db.migrate()
    return db


def _make_account(db: Database, **overrides: object) -> int:
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


def _patch_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """keyring 모듈을 인메모리 딱지로 바꾼다(test_keyring_store.py와 동일 패턴)."""
    store_data: dict[tuple[str, str], str] = {}

    def fake_set(service: str, key: str, value: str) -> None:
        store_data[(service, key)] = value

    def fake_get(service: str, key: str) -> str | None:
        return store_data.get((service, key))

    monkeypatch.setattr(keyring_store.keyring, "set_password", fake_set)
    monkeypatch.setattr(keyring_store.keyring, "get_password", fake_get)


def _seed(tmp_path: Path, server: FakeImapServer, monkeypatch: pytest.MonkeyPatch):
    """로컬 DB에 "이미 한 번 동기화된" 상태를 만든다(INBOX/Trash remote_name 포함)."""
    db = _make_db(tmp_path)
    _patch_keyring(monkeypatch)
    secret_store = KeyringSecretStore()

    account_id = _make_account(
        db,
        incoming_protocol="imap",
        in_host="127.0.0.1",
        in_port=server.port,
        in_security="none",
        in_username=server.username,
    )
    set_account_password(secret_store, account_id, server.password)

    inbox = folders_repo.get_or_create_folder(
        db, account_id, "INBOX", role="inbox", remote_name="INBOX"
    )
    folders_repo.get_or_create_folder(db, account_id, "Trash", role="trash", remote_name="Trash")

    remote_uid = server.add_message("INBOX", SIMPLE_EML)

    mail_dir = tmp_path / "mail"
    mail_dir.mkdir()
    eml_path = mail_dir / f"{uuid.uuid4()}.eml"
    eml_path.write_bytes(SIMPLE_EML)
    parsed = parse_eml(SIMPLE_EML)
    data = SyncService.to_message_insert(
        account_id=account_id,
        folder_id=inbox.id,
        parsed=parsed,
        remote_uid=str(remote_uid),
        eml_path=str(eml_path),
        received_at=datetime.now(UTC),
        is_read=False,
        is_flagged=False,
    )
    message_id = messages_repo.insert_message(db, data)

    api = UiApi(
        db=db,
        clock=SystemClock(),
        mail_blob_dir=mail_dir,
        secret_store=secret_store,
        sync_service=None,  # type: ignore[arg-type] — 이번 메서드들은 쓰지 않는다
        send_service=None,  # type: ignore[arg-type]
        draft_service=None,  # type: ignore[arg-type]
    )
    return db, api, account_id, message_id, remote_uid


@pytest.mark.parametrize(
    "support_move,support_uidplus",
    [(True, True), (False, True), (False, False)],
    ids=["move지원", "copy+uidplus", "copy만"],
)
def test_move_to_trash_reflects_on_imap_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, support_move: bool, support_uidplus: bool
) -> None:
    server = FakeImapServer(
        "testuser", "testpass", support_move=support_move, support_uidplus=support_uidplus
    )
    server.start()
    try:
        db, api, account_id, message_id, remote_uid = _seed(tmp_path, server, monkeypatch)
        try:
            asyncio.run(api.move_to_trash(message_id))

            # 서버 반영 확인: 항상 Trash에 복사/이동된다.
            assert len(server.mailboxes["Trash"].messages) == 1
            if support_uidplus:
                # UIDPLUS가 있으면(MOVE 지원 여부와 무관) UID EXPUNGE까지 되어
                # INBOX에서 완전히 사라진다(§2(b)).
                assert remote_uid not in server.mailboxes["INBOX"].messages
            else:
                # UIDPLUS가 없으면 다른 \Deleted 메일이 함께 지워지는 사고를 막기
                # 위해 EXPUNGE하지 않는다 — \Deleted 플래그만 남긴다(§2(b)).
                assert "\\Deleted" in server.mailboxes["INBOX"].messages[remote_uid]["flags"]

            # 로컬 DB 반영 확인: 휴지통 폴더로 옮겨지고 원래 폴더를 기억한다.
            conn = db.new_read_connection()
            try:
                row = messages_repo.get_message(conn, message_id)
                trash_folder = folders_repo.find_by_role(conn, account_id, "trash")
            finally:
                conn.close()
            assert row is not None
            assert trash_folder is not None
            assert row.folder_id == trash_folder.id
            assert row.original_folder_id is not None
        finally:
            db.stop()
    finally:
        server.stop()


def test_restore_from_trash_reflects_on_imap_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeImapServer("testuser", "testpass")
    server.start()
    try:
        db, api, account_id, message_id, remote_uid = _seed(tmp_path, server, monkeypatch)
        try:
            asyncio.run(api.move_to_trash(message_id))
            assert len(server.mailboxes["Trash"].messages) == 1

            asyncio.run(api.restore_from_trash(message_id))

            # 서버 반영 확인: Trash에서 빠지고 INBOX로 돌아갔다.
            assert len(server.mailboxes["Trash"].messages) == 0
            assert len(server.mailboxes["INBOX"].messages) == 1
        finally:
            db.stop()
    finally:
        server.stop()


def test_move_to_trash_pop3_account_is_local_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POP3 계정은 §2(b) 설계대로 서버 호출 없이 로컬만 바뀐다(회귀 방지)."""
    db = _make_db(tmp_path)
    _patch_keyring(monkeypatch)
    secret_store = KeyringSecretStore()
    account_id = _make_account(
        db,
        incoming_protocol="pop3",
        in_host="pop.example.com",
        in_port=995,
        in_security="ssl",
    )
    set_account_password(secret_store, account_id, "irrelevant")

    inbox = folders_repo.get_or_create_folder(db, account_id, "INBOX", role="inbox")
    mail_dir = tmp_path / "mail"
    mail_dir.mkdir()
    eml_path = mail_dir / f"{uuid.uuid4()}.eml"
    eml_path.write_bytes(SIMPLE_EML)
    parsed = parse_eml(SIMPLE_EML)
    data = SyncService.to_message_insert(
        account_id=account_id,
        folder_id=inbox.id,
        parsed=parsed,
        remote_uid="pop3-uid-1",
        eml_path=str(eml_path),
        received_at=datetime.now(UTC),
        is_read=False,
        is_flagged=False,
    )
    message_id = messages_repo.insert_message(db, data)

    api = UiApi(
        db=db,
        clock=SystemClock(),
        mail_blob_dir=mail_dir,
        secret_store=secret_store,
        sync_service=None,  # type: ignore[arg-type]
        send_service=None,  # type: ignore[arg-type]
        draft_service=None,  # type: ignore[arg-type]
    )
    # 서버가 없어도(host가 존재하지 않는 도메인이어도) 예외 없이 끝나야 한다 —
    # POP3는 IMAP 연결 시도 자체를 하지 않기 때문이다.
    asyncio.run(api.move_to_trash(message_id))

    conn = db.new_read_connection()
    try:
        row = messages_repo.get_message(conn, message_id)
    finally:
        conn.close()
    assert row is not None
    assert row.original_folder_id == inbox.id
    db.stop()
