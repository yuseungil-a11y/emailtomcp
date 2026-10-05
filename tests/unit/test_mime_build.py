"""MIME 생성 테스트 (DESIGN.md §1.2 #23·#24) — 전체회신 제외, 회신 첨부 기본 포함,
Re:/Fwd: 제목 정규화, In-Reply-To/References.
"""

from __future__ import annotations

from datetime import UTC, datetime

from emailtomcp.mail.mime import build as mime_build
from emailtomcp.mail.mime.build import OutgoingAttachment
from emailtomcp.mail.models import ParsedAddress, ParsedMessage


def _make_original(**overrides) -> ParsedMessage:
    base = {
        "message_id": "<orig-001@example.com>",
        "in_reply_to": None,
        "references": [],
        "subject": "회의 안내",
        "from_addrs": [ParsedAddress(name="김철수", addr="kim@example.com")],
        "sender_addr": None,
        "to_addrs": [
            ParsedAddress(name="나", addr="me@example.com"),
            ParsedAddress(name="박영희", addr="park@example.com"),
        ],
        "cc_addrs": [ParsedAddress(name="이민수", addr="lee@example.com")],
        "bcc_addrs": [],
        "reply_to_addrs": [],
        "date": datetime(2025, 9, 1, 9, 0, tzinfo=UTC),
        "body_text": "원본 본문입니다.",
        "body_source": "plain",
        "has_html": False,
        "content_mismatch": False,
        "attachments": [],
        "size_bytes": 100,
        "parse_limited": False,
        "body_truncated": False,
    }
    base.update(overrides)
    return ParsedMessage(**base)


def test_normalize_subject_removes_duplicate_prefixes() -> None:
    assert mime_build.normalize_subject("Re: Re: RE: 회의 안내", "Re:") == "Re: 회의 안내"
    assert mime_build.normalize_subject("회의 안내", "Fwd:") == "Fwd: 회의 안내"
    assert mime_build.normalize_subject(None, "Re:") == "Re:"


def test_build_new_sets_basic_headers_and_no_bcc_header() -> None:
    built = mime_build.build_new(
        from_addr="me@example.com",
        from_name="나",
        to_addrs=["a@example.com", "a@example.com"],
        cc_addrs=["b@example.com"],
        bcc_addrs=["secret@example.com"],
        subject="제목",
        body_text="본문",
    )
    assert built.to_addrs == ["a@example.com"]  # 중복 제거
    assert built.bcc_addrs == ["secret@example.com"]
    assert b"secret@example.com" not in built.raw_bytes  # Bcc 헤더 비노출
    assert b"To: a@example.com" in built.raw_bytes
    assert b"Message-ID:" in built.raw_bytes


def test_build_reply_targets_from_address_and_quotes_body() -> None:
    original = _make_original()
    built = mime_build.build_reply(
        original,
        from_addr="me@example.com",
        from_name="나",
        body_text="답장 본문",
    )
    assert built.to_addrs == ["kim@example.com"]
    assert built.cc_addrs == []
    assert built.subject == "Re: 회의 안내"
    assert "답장 본문" in built.body_text
    assert "> 원본 본문입니다." in built.body_text
    assert built.raw_bytes.find(b"In-Reply-To:") != -1


def test_build_reply_all_excludes_self_and_aliases() -> None:
    original = _make_original()
    built = mime_build.build_reply(
        original,
        from_addr="me@example.com",
        from_name="나",
        body_text="전체 답장",
        reply_all=True,
        self_aliases=["alias@example.com"],
    )
    to_norm = {a.lower() for a in built.to_addrs}
    assert "me@example.com" not in to_norm  # 내 주소 제외
    assert "kim@example.com" in to_norm  # 원 발신자
    assert "park@example.com" in to_norm  # 원 수신자(나 제외)
    assert built.cc_addrs == ["lee@example.com"]


def test_build_reply_includes_attachments_by_default() -> None:
    original = _make_original()
    attachment = OutgoingAttachment(
        filename="문서.pdf", content_type="application/pdf", payload=b"%PDF-1.4"
    )
    built = mime_build.build_reply(
        original,
        from_addr="me@example.com",
        from_name="나",
        body_text="첨부 포함 회신",
        original_attachments=[attachment],
    )
    assert b"JVBERi0xLjQ" in built.raw_bytes  # base64("%PDF-1.4")의 앞부분


def test_build_reply_can_exclude_attachments() -> None:
    original = _make_original()
    attachment = OutgoingAttachment(
        filename="문서.pdf", content_type="application/pdf", payload=b"%PDF-1.4"
    )
    built = mime_build.build_reply(
        original,
        from_addr="me@example.com",
        from_name="나",
        body_text="첨부 제외 회신",
        include_attachments=False,
        original_attachments=[attachment],
    )
    assert b"JVBERi0xLjQ" not in built.raw_bytes


def test_build_forward_inline_contains_original_body() -> None:
    original = _make_original()
    built = mime_build.build_forward_inline(
        original,
        from_addr="me@example.com",
        from_name="나",
        to_addrs=["dest@example.com"],
        body_text="전달합니다",
    )
    assert built.subject == "Fwd: 회의 안내"
    assert "원본 본문입니다." in built.body_text


def test_build_forward_attach_embeds_whole_eml() -> None:
    original = _make_original()
    raw_source = b"From: kim@example.com\r\nSubject: x\r\n\r\nbody\r\n"
    built = mime_build.build_forward_attach(
        original,
        raw_source,
        from_addr="me@example.com",
        from_name="나",
        to_addrs=["dest@example.com"],
        body_text="첨부로 전달합니다",
    )
    assert built.subject == "Fwd: 회의 안내"
    assert b"message/rfc822" in built.raw_bytes
