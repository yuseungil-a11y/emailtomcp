"""LoopGuard 정적 조건 1~11 (DESIGN.md §7.3) — P3 Phase B.

G1 원자료 추출(mail/loop_headers.py) → LoopGuard 입력(rules/engine.build_loop_input) →
check_static을 실제 경로대로 거친다. 조건마다 1종 이상 + 통과 케이스 + 원자료 없음(fail-closed).
"""

from __future__ import annotations

import pytest

from emailtomcp.mail.addressing import normalize_addr
from emailtomcp.mail.loop_headers import extract_loop_headers
from emailtomcp.mail.mime.parse import parse_eml
from emailtomcp.rules import loop_guard as lg
from emailtomcp.rules.engine import build_loop_input
from emailtomcp.storage.repositories.messages import AutoReplyFacts

pytestmark = pytest.mark.unit

MINE = frozenset({"me@example.com", "alias@example.com", "other-account@corp.example"})


def _facts(raw: bytes) -> AutoReplyFacts:
    parsed = parse_eml(raw)
    from_addr = parsed.from_addrs[0].addr if parsed.from_addrs else None
    return AutoReplyFacts(
        id=1,
        account_id=1,
        folder_id=1,
        folder_role="inbox",
        from_addr=from_addr,
        from_name=None,
        from_addr_norm=normalize_addr(from_addr) if from_addr else None,
        from_count=len(parsed.from_addrs),
        sender_addr=parsed.sender_addr.addr if parsed.sender_addr else None,
        to_addrs_json="[]",
        cc_addrs_json="[]",
        reply_to_json="[]",
        subject=parsed.subject,
        body_text=parsed.body_text,
        has_attachments=False,
        content_mismatch=False,
        date_hdr=None,
        received_at="2026-10-05T00:00:00+00:00",
        thread_key=None,
        auto_headers_json=None,
        auto_reply_status=None,
        eml_path="x",
    )


def _check(raw: bytes) -> list[str]:
    headers = extract_loop_headers(raw, is_backfill=False)
    return lg.check_static(build_loop_input(_facts(raw), headers, normalize_addr), MINE)


def _mail(
    headers: str = "",
    *,
    frm: str = "partner@client.example",
    subject: str = "견적 문의",
    content_type: str = "text/plain; charset=utf-8",
    body: str = "본문",
) -> bytes:
    return (
        f"From: {frm}\r\nTo: me@example.com\r\nSubject: {subject}\r\n"
        f"Content-Type: {content_type}\r\n{headers}\r\n{body}\r\n"
    ).encode()


def test_clean_mail_passes() -> None:
    assert _check(_mail()) == []


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ("Auto-Submitted: auto-replied\r\n", lg.R1_AUTO_SUBMITTED),
        ("Auto-Submitted: auto-generated; owner=x\r\n", lg.R1_AUTO_SUBMITTED),
        ("Precedence: bulk\r\n", lg.R2_PRECEDENCE),
        ("Precedence: auto_reply\r\n", lg.R2_PRECEDENCE),
        ("List-Id: <news.list.example>\r\n", lg.R3_LIST_HEADERS),
        ("List-Unsubscribe: <mailto:u@list.example>\r\n", lg.R3_LIST_HEADERS),
        ("Feedback-ID: 1:2:3\r\n", lg.R3_LIST_HEADERS),
        ("X-Auto-Response-Suppress: OOF, DR\r\n", lg.R4_AUTO_RESPONSE_SUPPRESS),
        ("X-Auto-Response-Suppress: All\r\n", lg.R4_AUTO_RESPONSE_SUPPRESS),
        ("X-Autoreply: yes\r\n", lg.R5_AUTOREPLY_HEADER),
        ("X-Autorespond: 1\r\n", lg.R5_AUTOREPLY_HEADER),
        ("Return-Path: <>\r\n", lg.R6_NULL_SENDER_OR_ROBOT),
        ("X-Mailer: Vacation Responder 2.0\r\n", lg.R8_AUTORESPONDER_AGENT),
        ("User-Agent: AutoReply-Bot\r\n", lg.R8_AUTORESPONDER_AGENT),
        ("X-EmailToMCP-Auto: 1\r\n", lg.R10_SELF_SENT),
        ("Sender: someone-else@client.example\r\n", lg.R11_FROM_SENDER),
    ],
)
def test_header_conditions_block(headers: str, expected: str) -> None:
    assert expected in _check(_mail(headers))


def test_auto_submitted_no_passes() -> None:
    assert lg.R1_AUTO_SUBMITTED not in _check(_mail("Auto-Submitted: no\r\n"))


def test_sender_same_as_from_passes() -> None:
    assert _check(_mail("Sender: Partner@CLIENT.example\r\n")) == []


@pytest.mark.parametrize(
    "frm",
    [
        "MAILER-DAEMON@mx.example",
        "noreply@shop.example",
        "no-reply@x.example",
        "bounce-123@lists.example",
        "postmaster@client.example",
    ],
)
def test_robot_localparts_block(frm: str) -> None:
    assert lg.R6_NULL_SENDER_OR_ROBOT in _check(_mail(frm=frm))


def test_multipart_report_and_dsn_parts_block() -> None:
    report = _mail(
        content_type='multipart/report; report-type=delivery-status; boundary="b"',
        body="--b\r\nContent-Type: text/plain\r\n\r\nx\r\n--b--",
    )
    assert lg.R7_REPORT in _check(report)
    mixed = _mail(
        content_type='multipart/mixed; boundary="b"',
        body="--b\r\nContent-Type: message/disposition-notification\r\n\r\nx\r\n--b--",
    )
    assert lg.R7_REPORT in _check(mixed)


@pytest.mark.parametrize(
    "subject",
    [
        "Out of Office: 휴가",
        "Automatic reply: 확인",
        "자동 응답 메일",
        "[부재중] 안내",
        "Undeliverable: test",
        "Delivery Status Notification (Failure)",
        "메일 반송 안내",
        "ＯＵＴ ＯＦ ＯＦＦＩＣＥ",
    ],
)
def test_subject_patterns_block(subject: str) -> None:
    assert lg.R9_SUBJECT_PATTERN in _check(_mail(subject=subject))


def test_self_sent_including_other_accounts_and_aliases() -> None:
    assert lg.R10_SELF_SENT in _check(_mail(frm="Me@Example.com"))
    assert lg.R10_SELF_SENT in _check(_mail(frm="alias@example.com"))
    assert lg.R10_SELF_SENT in _check(_mail(frm="other-account@corp.example"))  # 계정 간 루프


def test_multiple_or_missing_from_blocks() -> None:
    assert lg.R11_FROM_SENDER in _check(_mail(frm="a@x.example, b@y.example"))
    no_from = b"To: me@example.com\r\nSubject: x\r\n\r\nbody\r\n"
    assert lg.R11_FROM_SENDER in _check(no_from)


def test_missing_raw_headers_is_blocked() -> None:
    raw = _mail()
    reasons = lg.check_static(build_loop_input(_facts(raw), None, normalize_addr), MINE)
    assert reasons == [lg.R_HEADERS_UNAVAILABLE]


def test_all_reasons_are_reported() -> None:
    raw = _mail("Precedence: bulk\r\nList-Id: <l>\r\n", subject="Out of office")
    reasons = _check(raw)
    assert {lg.R2_PRECEDENCE, lg.R3_LIST_HEADERS, lg.R9_SUBJECT_PATTERN} <= set(reasons)
