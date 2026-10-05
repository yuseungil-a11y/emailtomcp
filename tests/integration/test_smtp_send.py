"""SMTP 발신 통합 테스트 (aiosmtpd, DESIGN.md §8.7, §1.2 #24 In-Reply-To/References,
Bcc 비노출).
"""

from __future__ import annotations

import socket

import pytest
from aiosmtpd.controller import Controller

from emailtomcp.core.errors import PermanentError
from emailtomcp.mail.mime import build as mime_build
from emailtomcp.mail.outgoing import smtp as smtp_outgoing

pytestmark = pytest.mark.integration


class _RecordingHandler:
    def __init__(self) -> None:
        self.envelopes: list[dict] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802, ANN001 — aiosmtpd 콜백 시그니처
        self.envelopes.append(
            {
                "mail_from": envelope.mail_from,
                "rcpt_tos": list(envelope.rcpt_tos),
                "content": bytes(envelope.content),
            }
        )
        return "250 OK"


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def smtp_server():
    handler = _RecordingHandler()
    port = _unused_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    yield handler, port
    controller.stop()


def test_send_mail_delivers_message(smtp_server) -> None:
    handler, port = smtp_server
    built = mime_build.build_new(
        from_addr="me@example.com",
        from_name="나",
        to_addrs=["dest@example.com"],
        cc_addrs=["cc@example.com"],
        bcc_addrs=["bcc-secret@example.com"],
        subject="테스트 발송",
        body_text="본문입니다",
    )
    rcpt_tos = [*built.to_addrs, *built.cc_addrs, *built.bcc_addrs]

    smtp_outgoing.send_mail(
        host="127.0.0.1",
        port=port,
        username=None,
        password=None,
        security="none",
        mail_from="me@example.com",
        rcpt_tos=rcpt_tos,
        raw_bytes=built.raw_bytes,
    )

    assert len(handler.envelopes) == 1
    envelope = handler.envelopes[0]
    assert envelope["mail_from"] == "me@example.com"
    # Bcc 수신자는 SMTP RCPT TO에는 포함되지만, 메시지 본문/헤더에는 노출되지 않는다.
    assert "bcc-secret@example.com" in envelope["rcpt_tos"]
    assert b"bcc-secret@example.com" not in envelope["content"]
    assert b"Bcc:" not in envelope["content"]


def test_send_mail_reply_headers_present(smtp_server) -> None:
    handler, port = smtp_server
    built = mime_build.build_new(
        from_addr="me@example.com",
        from_name="나",
        to_addrs=["dest@example.com"],
        subject="Re: 원본 제목",
        body_text="회신 본문",
        in_reply_to="<orig-001@example.com>",
        references=["<orig-001@example.com>"],
    )

    smtp_outgoing.send_mail(
        host="127.0.0.1",
        port=port,
        username=None,
        password=None,
        security="none",
        mail_from="me@example.com",
        rcpt_tos=built.to_addrs,
        raw_bytes=built.raw_bytes,
    )

    envelope = handler.envelopes[0]
    assert b"In-Reply-To: <orig-001@example.com>" in envelope["content"]
    assert b"References: <orig-001@example.com>" in envelope["content"]


def test_send_mail_starttls_not_advertised_raises_permanent_error(smtp_server) -> None:
    """aiosmtpd 기본 핸들러는 STARTTLS를 광고하지 않는다 — 평문 다운그레이드 없이
    즉시 실패해야 한다(§8.7 M7)."""
    _handler, port = smtp_server
    with pytest.raises(PermanentError):
        smtp_outgoing.send_mail(
            host="127.0.0.1",
            port=port,
            username=None,
            password=None,
            security="starttls",
            mail_from="me@example.com",
            rcpt_tos=["dest@example.com"],
            raw_bytes=b"Subject: x\r\n\r\nbody\r\n",
        )
