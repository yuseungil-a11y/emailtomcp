"""MIME 파싱 테스트 — 한글 인코딩 fixture 8종 (갈릴레오 §5, DESIGN.md §9.4 §8.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from emailtomcp.mail.mime.parse import parse_eml

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eml"


def _load(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def test_utf8_plain() -> None:
    parsed = parse_eml(_load("utf8_plain.eml"))
    assert not parsed.parse_limited
    assert "안녕하세요" in (parsed.body_text or "")
    assert parsed.subject == "UTF-8 plain test"


def test_euckr_plain() -> None:
    parsed = parse_eml(_load("euckr_plain.eml"))
    assert "안녕하세요" in (parsed.body_text or "")
    assert "EUC-KR" in (parsed.body_text or "")


def test_cp949_plain() -> None:
    parsed = parse_eml(_load("cp949_plain.eml"))
    assert "안녕하세요" in (parsed.body_text or "")
    assert "뀨뀰" in (parsed.body_text or "")


def test_ks_c_5601_alias_mapping() -> None:
    """비표준 charset 라벨(ks_c_5601-1987)이 cp949로 정상 매핑되는지 확인한다(§9.4)."""
    parsed = parse_eml(_load("ks_c_5601_plain.eml"))
    assert "안녕하세요" in (parsed.body_text or "")
    assert "�" not in (parsed.body_text or "")


def test_rfc2231_attachment_filename() -> None:
    parsed = parse_eml(_load("rfc2231_attachment.eml"))
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.filename_raw == "한글파일.txt"


def test_nonstandard_encoded_word_filename() -> None:
    parsed = parse_eml(_load("nonstandard_encoded_word_filename.eml"))
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.filename_raw == "견적서.xlsx"


def test_folded_subject() -> None:
    parsed = parse_eml(_load("folded_subject.eml"))
    assert parsed.subject == "매우 길고 긴 제목의 앞부분입니다 그리고 이어지는 뒷부분입니다"


def test_content_mismatch_detected() -> None:
    parsed = parse_eml(_load("content_mismatch.eml"))
    assert parsed.has_html is True
    assert parsed.content_mismatch is True
    assert parsed.body_source == "html"


def test_raw8bit_utf8_header_not_limited() -> None:
    """QA BUG-① 재현: RFC 2047 인코딩 없이 UTF-8 바이트 그대로인 From/Subject 헤더.

    `email.message_from_bytes`가 이런 헤더를 `email.header.Header` 객체로 돌려줘
    `_unfold`가 TypeError로 죽고 `parse_limited=True`로 정보가 소실되던 버그.
    """
    parsed = parse_eml(_load("raw8bit_utf8_header.eml"))
    assert not parsed.parse_limited
    assert parsed.message_id == "<raw8bit-utf8-009@example.com>"
    assert len(parsed.from_addrs) == 1
    assert parsed.from_addrs[0].addr == "sender@example.com"
    assert parsed.from_addrs[0].name == "미안"
    assert parsed.subject == "한글 제목 테스트 메일입니다"
    assert "안녕하세요" in (parsed.body_text or "")


def test_raw8bit_euckr_header_not_limited() -> None:
    """QA BUG-① 권고 2종 중 EUC-KR 버전: 인코딩 없이 EUC-KR 바이트 그대로인 헤더."""
    parsed = parse_eml(_load("raw8bit_euckr_header.eml"))
    assert not parsed.parse_limited
    assert parsed.message_id == "<raw8bit-euckr-010@example.com>"
    assert len(parsed.from_addrs) == 1
    assert parsed.from_addrs[0].addr == "sender2@example.com"
    assert parsed.from_addrs[0].name == "한국도로공사 담당자"
    assert parsed.subject == "한국도로공사 담당자 앞 테스트 메일 제목"
    assert "본문 내용입니다" in (parsed.body_text or "")


def test_raw8bit_attachment_filename_not_limited() -> None:
    """QA D-1 재현: 첨부 파트의 Content-Disposition filename과 Content-ID가
    RFC 2047 인코딩 없이 UTF-8 바이트 그대로인 경우.

    `(part.get("Content-Disposition") or "").lower()`와 `content_id.strip()`이
    Header 객체를 받아 AttributeError로 죽고 메일 전체가 `parse_limited=True`로
    강등되며 본문·첨부가 전부 사라지던 버그(parse.py:285, 296).
    """
    parsed = parse_eml(_load("raw8bit_attachment_filename.eml"))
    assert not parsed.parse_limited
    assert "Raw 8bit attachment filename" in (parsed.body_text or "")
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.filename_raw == "한글문서.pdf"
    assert att.content_id == "첨부식별자"


def test_raw8bit_contenttype_filename_decoded() -> None:
    """QA D-2 재현: 파일명이 Content-Disposition이 아니라 Content-Type의
    `name=` 파라미터에만 있고, 그 값이 인코딩 없이 UTF-8 바이트 그대로인 경우.
    크래시는 없었지만 `get_filename()`이 돌려주는 surrogateescape 문자열을
    그대로 썼기 때문에 깨진 문자열(`'������.jpg'`)이 나오던 문제."""
    parsed = parse_eml(_load("raw8bit_contenttype_filename.eml"))
    assert not parsed.parse_limited
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.filename_raw == "첨부사진.jpg"


def test_raw8bit_cd_ascii_filename_takes_priority_over_ct_8bit_name() -> None:
    """QA R-1 재현: Content-Disposition filename이 ASCII(str)이고 Content-Type name이
    8비트일 때, `Message.get_filename()`과 동일하게 Content-Disposition을 우선해야
    한다. 수정 전에는 Content-Type 쪽("다른이름.pdf")이 선택됐다."""
    parsed = parse_eml(_load("raw8bit_cd_ascii_ct_8bit_filename_priority.eml"))
    assert not parsed.parse_limited
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert att.filename_raw == "ascii.pdf"


@pytest.mark.parametrize(
    "name",
    [
        "utf8_plain.eml",
        "euckr_plain.eml",
        "cp949_plain.eml",
        "ks_c_5601_plain.eml",
        "rfc2231_attachment.eml",
        "nonstandard_encoded_word_filename.eml",
        "folded_subject.eml",
        "content_mismatch.eml",
        "raw8bit_utf8_header.eml",
        "raw8bit_euckr_header.eml",
        "raw8bit_attachment_filename.eml",
        "raw8bit_contenttype_filename.eml",
        "raw8bit_cd_ascii_ct_8bit_filename_priority.eml",
    ],
)
def test_never_raises(name: str) -> None:
    """어떤 fixture로도 예외를 던지지 않는다(parse_eml의 계약)."""
    parsed = parse_eml(_load(name))
    assert parsed is not None
