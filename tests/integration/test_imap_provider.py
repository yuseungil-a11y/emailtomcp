"""IMAP provider 통합 테스트 (fake 서버 기반, DESIGN.md §2(b) N-01).

이동(move)은 서버 능력에 따라 세 구성으로 테스트한다:
1. MOVE 지원(+UIDPLUS)
2. MOVE 미지원, UIDPLUS만 지원(COPY+`\\Deleted`+UID EXPUNGE)
3. MOVE·UIDPLUS 모두 미지원(COPY+`\\Deleted`만, EXPUNGE 안 함)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
from fake_imap_server import FakeImapServer  # noqa: E402

from emailtomcp.mail.incoming.imap import ImapIncomingProvider  # noqa: E402

pytestmark = pytest.mark.integration


def _make_provider(server: FakeImapServer) -> ImapIncomingProvider:
    return ImapIncomingProvider(
        host="127.0.0.1",
        port=server.port,
        username="testuser",
        password="testpass",
        security="none",
    )


def _start_server(**kwargs) -> FakeImapServer:
    server = FakeImapServer("testuser", "testpass", **kwargs)
    server.start()
    return server


def test_list_folders_resolves_special_use_roles() -> None:
    server = _start_server()
    try:
        provider = _make_provider(server)
        provider.connect()
        try:
            folders = provider.list_folders()
            roles = {f.name: f.role for f in folders}
            assert roles["INBOX"] == "inbox"
            assert roles["Trash"] == "trash"
            assert roles["Sent"] == "sent"
        finally:
            provider.close()
    finally:
        server.stop()


def test_list_new_and_fetch_without_marking_seen() -> None:
    server = _start_server()
    try:
        uid = server.add_message("INBOX", b"Subject: hello\r\n\r\nbody text\r\n")
        provider = _make_provider(server)
        provider.connect()
        try:
            summaries = provider.list_new("INBOX", known_uids=set())
            assert {s.remote_uid for s in summaries} == {str(uid)}

            fetched = provider.fetch("INBOX", str(uid))
            assert b"body text" in fetched.raw
            assert "Seen" not in fetched.flags  # BODY.PEEK[]는 \Seen을 세우지 않는다
        finally:
            provider.close()
    finally:
        server.stop()


def test_set_flags_seen_and_flagged() -> None:
    server = _start_server()
    try:
        uid = server.add_message("INBOX", b"Subject: x\r\n\r\nbody\r\n")
        provider = _make_provider(server)
        provider.connect()
        try:
            provider.set_flags("INBOX", str(uid), seen=True, flagged=True)
            fetched = provider.fetch("INBOX", str(uid))
            flag_names = {f.lstrip("\\") for f in fetched.flags}
            assert "Seen" in flag_names
            assert "Flagged" in flag_names
        finally:
            provider.close()
    finally:
        server.stop()


def test_move_with_move_capability() -> None:
    """MOVE(RFC6851)로 실제 이동은 성공한다.

    새 UID(COPYUID)는 `imapclient`가 `use_uid=True`일 때 `imaplib.IMAP4.uid()`를 거쳐
    호출하면서 태그된 완료 응답을 버리는 알려진 제약으로 항상 `None`이 된다
    (`ImapIncomingProvider.move` 문서 참조) — 그래서 여기서는 이동 자체(원본에서
    없어지고 대상에 생김)만 검증한다.
    """
    server = _start_server(support_move=True, support_uidplus=True)
    try:
        uid = server.add_message("INBOX", b"Subject: move me\r\n\r\nbody\r\n")
        provider = _make_provider(server)
        provider.connect()
        try:
            provider.move("INBOX", str(uid), "Trash")
            assert uid not in server.mailboxes["INBOX"].messages
            assert len(server.mailboxes["Trash"].messages) == 1
        finally:
            provider.close()
    finally:
        server.stop()


def test_move_without_move_but_with_uidplus_expunges_target_uid_only() -> None:
    server = _start_server(support_move=False, support_uidplus=True)
    try:
        uid_keep = server.add_message("INBOX", b"Subject: keep\r\n\r\nbody keep\r\n")
        uid_move = server.add_message("INBOX", b"Subject: move\r\n\r\nbody move\r\n")
        provider = _make_provider(server)
        provider.connect()
        try:
            provider.move("INBOX", str(uid_move), "Trash")
            # UIDPLUS가 있으므로 이동한 메일만 원본 폴더에서 사라진다.
            assert uid_move not in server.mailboxes["INBOX"].messages
            assert uid_keep in server.mailboxes["INBOX"].messages
            assert len(server.mailboxes["Trash"].messages) == 1
        finally:
            provider.close()
    finally:
        server.stop()


def test_move_without_move_and_without_uidplus_only_flags_deleted() -> None:
    server = _start_server(support_move=False, support_uidplus=False)
    try:
        uid_keep = server.add_message("INBOX", b"Subject: keep\r\n\r\nbody keep\r\n")
        uid_move = server.add_message("INBOX", b"Subject: move\r\n\r\nbody move\r\n")
        provider = _make_provider(server)
        provider.connect()
        try:
            new_uid = provider.move("INBOX", str(uid_move), "Trash")
            # COPYUID가 없으니 새 UID를 알 수 없다 — 호출자가 다음 동기화에서 매칭해야 한다.
            assert new_uid is None
            # EXPUNGE를 하지 않았으므로 원본 폴더에 \Deleted만 남고 행은 그대로 있다.
            assert uid_move in server.mailboxes["INBOX"].messages
            assert "\\Deleted" in server.mailboxes["INBOX"].messages[uid_move]["flags"]
            assert uid_keep in server.mailboxes["INBOX"].messages
            # 그래도 대상 폴더에는 복사됐다.
            assert len(server.mailboxes["Trash"].messages) == 1
        finally:
            provider.close()
    finally:
        server.stop()


def test_append_message_to_sent() -> None:
    server = _start_server()
    try:
        provider = _make_provider(server)
        provider.connect()
        try:
            provider.append_message("Sent", b"Subject: sent copy\r\n\r\nbody\r\n")
            assert len(server.mailboxes["Sent"].messages) == 1
        finally:
            provider.close()
    finally:
        server.stop()
