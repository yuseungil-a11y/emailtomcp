"""POP3 provider 통합 테스트 (fake 서버 기반, DESIGN.md §2(b))."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
from fake_pop3_server import FakePop3Server  # noqa: E402

from emailtomcp.mail.incoming.pop3 import Pop3IncomingProvider  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture
def pop3_server():
    server = FakePop3Server("testuser", "testpass")
    server.start()
    yield server
    server.stop()


def _make_provider(server: FakePop3Server) -> Pop3IncomingProvider:
    return Pop3IncomingProvider(
        host="127.0.0.1",
        port=server.port,
        username="testuser",
        password="testpass",
        security="none",
    )


def test_list_new_and_fetch(pop3_server: FakePop3Server) -> None:
    pop3_server.add_message("uidl-1", b"Subject: one\r\n\r\nbody one\r\n")
    pop3_server.add_message("uidl-2", b"Subject: two\r\n\r\nbody two\r\n")

    provider = _make_provider(pop3_server)
    provider.connect()
    try:
        summaries = provider.list_new("INBOX", known_uids=set())
        assert {s.remote_uid for s in summaries} == {"uidl-1", "uidl-2"}

        fetched = provider.fetch("INBOX", "uidl-1")
        assert b"body one" in fetched.raw
    finally:
        provider.close()


def test_known_uids_are_filtered_out(pop3_server: FakePop3Server) -> None:
    pop3_server.add_message("uidl-1", b"Subject: one\r\n\r\nbody one\r\n")
    pop3_server.add_message("uidl-2", b"Subject: two\r\n\r\nbody two\r\n")

    provider = _make_provider(pop3_server)
    provider.connect()
    try:
        summaries = provider.list_new("INBOX", known_uids={"uidl-1"})
        assert {s.remote_uid for s in summaries} == {"uidl-2"}
    finally:
        provider.close()


def test_delete_removes_message_from_server(pop3_server: FakePop3Server) -> None:
    pop3_server.add_message("uidl-1", b"Subject: one\r\n\r\nbody one\r\n")

    provider = _make_provider(pop3_server)
    provider.connect()
    try:
        provider.list_new("INBOX", known_uids=set())
        provider.delete("INBOX", "uidl-1")
    finally:
        provider.close()

    # 두 번째 연결로 다시 확인한다(QUIT으로 삭제가 커밋됐는지).
    provider2 = _make_provider(pop3_server)
    provider2.connect()
    try:
        summaries = provider2.list_new("INBOX", known_uids=set())
        assert summaries == []
    finally:
        provider2.close()


def test_move_is_noop_for_pop3(pop3_server: FakePop3Server) -> None:
    pop3_server.add_message("uidl-1", b"Subject: one\r\n\r\nbody\r\n")
    provider = _make_provider(pop3_server)
    provider.connect()
    try:
        provider.list_new("INBOX", known_uids=set())
        result = provider.move("INBOX", "uidl-1", "Trash")
        assert result is None
    finally:
        provider.close()


def test_wrong_password_raises_auth_error(pop3_server: FakePop3Server) -> None:
    from emailtomcp.core.errors import AuthError

    provider = Pop3IncomingProvider(
        host="127.0.0.1",
        port=pop3_server.port,
        username="testuser",
        password="wrong",
        security="none",
    )
    with pytest.raises(AuthError):
        provider.connect()
