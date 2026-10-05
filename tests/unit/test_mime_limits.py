"""MIME 상한(DESIGN.md §8.2 L5) — 초과 시 크래시 없이 "파싱 제한" 모드로 전환."""

from __future__ import annotations

from emailtomcp.mail.mime import limits
from emailtomcp.mail.mime.parse import parse_eml


def _simple_eml(body: bytes, *, subject: str = "limit test") -> bytes:
    return (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: " + subject.encode("ascii") + b"\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <limit-test@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body
    )


def test_raw_size_over_limit_sets_parse_limited() -> None:
    oversized_body = b"A" * (limits.MAX_RAW_BYTES + 1024)
    raw = _simple_eml(oversized_body)
    parsed = parse_eml(raw)
    assert parsed.parse_limited is True
    # 상한 초과 시에도 헤더 요약(제목)은 가능하면 보존한다.
    assert parsed.subject == "limit test"


def test_single_header_over_limit_sets_parse_limited() -> None:
    huge_subject = "A" * (limits.MAX_SINGLE_HEADER_BYTES + 100)
    raw = _simple_eml(b"hello", subject=huge_subject)
    parsed = parse_eml(raw)
    assert parsed.parse_limited is True


def test_too_many_parts_sets_parse_limited() -> None:
    boundary = b"BOUNDARY-LIMIT"
    parts = []
    for i in range(limits.MAX_PARTS + 5):
        parts.append(
            b"--" + boundary + b"\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Disposition: attachment; filename=part" + str(i).encode("ascii") + b".txt\r\n"
            b"\r\n"
            b"x\r\n"
        )
    body = b"".join(parts) + b"--" + boundary + b"--\r\n"
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: too many parts\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <limit-parts@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="' + boundary + b'"\r\n'
        b"\r\n" + body
    )
    parsed = parse_eml(raw)
    assert parsed.parse_limited is True


def test_db_body_clamped_to_2mb() -> None:
    long_text = "가" * 2_000_000
    clamped, truncated = limits.clamp_db_body(long_text)
    assert truncated is True
    assert len(clamped.encode("utf-8")) <= limits.MAX_DB_BODY_BYTES


def test_db_body_not_clamped_when_small() -> None:
    clamped, truncated = limits.clamp_db_body("짧은 본문")
    assert truncated is False
    assert clamped == "짧은 본문"


def test_check_header_block_size_raises_over_limit() -> None:
    limits.check_header_block_size(limits.MAX_HEADERS_TOTAL_BYTES)  # 상한 자체는 허용
    try:
        limits.check_header_block_size(limits.MAX_HEADERS_TOTAL_BYTES + 1)
        raise AssertionError("LimitExceeded가 나야 한다")
    except limits.LimitExceeded as exc:
        assert "헤더 총합 상한 초과" in exc.reason


def test_check_nesting_depth_raises_over_limit() -> None:
    limits.check_nesting_depth(limits.MAX_NESTING_DEPTH)  # 상한 자체는 허용
    try:
        limits.check_nesting_depth(limits.MAX_NESTING_DEPTH + 1)
        raise AssertionError("LimitExceeded가 나야 한다")
    except limits.LimitExceeded as exc:
        assert "중첩 깊이 상한 초과" in exc.reason


def test_check_decoded_total_raises_over_limit() -> None:
    limits.check_decoded_total(limits.MAX_DECODED_TOTAL_BYTES)  # 상한 자체는 허용
    try:
        limits.check_decoded_total(limits.MAX_DECODED_TOTAL_BYTES + 1)
        raise AssertionError("LimitExceeded가 나야 한다")
    except limits.LimitExceeded as exc:
        assert "디코딩 후 총합 상한 초과" in exc.reason
