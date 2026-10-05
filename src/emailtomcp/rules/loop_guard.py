"""LoopGuard — 자동회신 루프·대량메일·반송 차단 (DESIGN.md §7.3) — P3 Phase B.

**정적 조건 1~11**(`check_static`)은 메일만 보고 판정하는 순수 함수다. 끌 수 없고 규칙보다 먼저
적용된다(§7.2 2단계). 하나라도 해당하면 차단하며, 판정 오류는 언제나 "차단" 쪽으로 기운다.

동적 조건 12~14(스레드 횟수·쿨다운·회로차단)는 auto_send에만 의미가 있어 Phase C(G7 권위
판정)에서 `check_dynamic`으로 추가한다 — Phase B의 결과는 모두 사람이 검토하는 초안이다.

입력(`LoopGuardInput`)은 G1이 저장한 헤더 원자료(mail/loop_headers.py)와 메일 행에서 만든다.
주소 정규화는 호출자가 끝내서 넘긴다(§4.2: rules는 mail을 import하지 않는다).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# 사유 코드(auto_reply_log.reason_code). 번호는 §7.3 표.
R1_AUTO_SUBMITTED = "loop_auto_submitted"
R2_PRECEDENCE = "loop_precedence"
R3_LIST_HEADERS = "loop_list_headers"
R4_AUTO_RESPONSE_SUPPRESS = "loop_auto_response_suppress"
R5_AUTOREPLY_HEADER = "loop_autoreply_header"
R6_NULL_SENDER_OR_ROBOT = "loop_null_sender_or_robot"
R7_REPORT = "loop_report"
R8_AUTORESPONDER_AGENT = "loop_autoresponder_agent"
R9_SUBJECT_PATTERN = "loop_subject_pattern"
R10_SELF_SENT = "self_sent"
R11_FROM_SENDER = "from_sender_mismatch"
R_HEADERS_UNAVAILABLE = "loop_headers_unavailable"

_PRECEDENCE_BLOCK = frozenset({"bulk", "list", "junk", "auto_reply", "auto-reply"})
_LIST_HEADERS = ("list-id", "list-unsubscribe", "list-post", "feedback-id")
_SUPPRESS_TOKENS = frozenset({"all", "autoreply", "oof", "dr"})
_AUTOREPLY_HEADERS = (
    "x-autoreply",
    "x-autorespond",
    "x-auto-reply",
    "x-autoreply-from",
    "x-mail-autoreply",
)
_ROBOT_LOCALPARTS = frozenset(
    {"mailer-daemon", "postmaster", "noreply", "no-reply", "donotreply", "do-not-reply"}
)
# 알려진 자동응답기 X-Mailer/User-Agent(부분일치, 소문자). 오탐은 "차단"이라 넓게 잡는다.
AUTORESPONDER_AGENT_PATTERNS: tuple[str, ...] = (
    "autorespond",
    "auto-respond",
    "auto respond",
    "autoreply",
    "auto-reply",
    "auto reply",
    "vacation",
    "out of office",
    "outofoffice",
    "responder",
    "mailer-daemon",
)
# 제목 패턴(대소문자 무시, NFKC 후 부분일치, §7.3 9번).
SUBJECT_PATTERNS: tuple[str, ...] = (
    "out of office",
    "automatic reply",
    "auto-reply",
    "autoreply",
    "자동 응답",
    "자동응답",
    "부재중",
    "undeliverable",
    "undelivered",
    "delivery status notification",
    "returned mail",
    "mail delivery failed",
    "반송",
)


@dataclass(frozen=True, slots=True)
class LoopGuardInput:
    headers: Mapping[str, Sequence[str]]  # 이름은 소문자(G1 원자료)
    content_type: str
    report_parts: bool
    from_addr_norm: str | None
    from_count: int
    sender_addr_norm: str | None
    subject: str | None
    parse_error: bool = False


def _values(headers: Mapping[str, Sequence[str]], name: str) -> list[str]:
    raw = headers.get(name) or []
    return [str(v) for v in raw]


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _localpart(addr_norm: str | None) -> str:
    if not addr_norm or "@" not in addr_norm:
        return ""
    return addr_norm.rpartition("@")[0]


def check_static(msg: LoopGuardInput, my_addresses: frozenset[str]) -> list[str]:
    """정적 조건 1~11 중 해당하는 사유 코드를 모두 돌려준다(빈 목록이면 통과).

    Args:
        my_addresses: **모든 내 계정의 주소와 별칭**(정규화된 addr-spec). 계정 간 루프 포함(10번).
    """
    if msg.parse_error:
        return [R_HEADERS_UNAVAILABLE]
    headers = msg.headers
    reasons: list[str] = []

    # 1. Auto-Submitted가 있고 값이 no가 아님(빈 값도 차단)
    for value in _values(headers, "auto-submitted"):
        if value.split(";", 1)[0].strip().lower() != "no":
            reasons.append(R1_AUTO_SUBMITTED)
            break

    # 2. Precedence: bulk/list/junk/auto_reply
    if any(v.strip().lower() in _PRECEDENCE_BLOCK for v in _values(headers, "precedence")):
        reasons.append(R2_PRECEDENCE)

    # 3. 메일링 리스트·대량발송 헤더
    if any(headers.get(name) for name in _LIST_HEADERS):
        reasons.append(R3_LIST_HEADERS)

    # 4. X-Auto-Response-Suppress에 All/AutoReply/OOF/DR
    for value in _values(headers, "x-auto-response-suppress"):
        tokens = {t.strip().lower() for t in value.replace(";", ",").split(",")}
        if tokens & _SUPPRESS_TOKENS:
            reasons.append(R4_AUTO_RESPONSE_SUPPRESS)
            break

    # 5. 자동응답 표식 헤더
    if any(headers.get(name) for name in _AUTOREPLY_HEADERS):
        reasons.append(R5_AUTOREPLY_HEADER)

    # 6. Return-Path: <> 또는 로봇 발신 로컬파트(bounce* 포함)
    null_return = any(
        v.strip().replace(" ", "") in ("<>", "") for v in _values(headers, "return-path")
    )
    local = _localpart(msg.from_addr_norm)
    if null_return or local in _ROBOT_LOCALPARTS or local.startswith("bounce"):
        reasons.append(R6_NULL_SENDER_OR_ROBOT)

    # 7. DSN·MDN(multipart/report 또는 delivery-status/disposition-notification 파트)
    if msg.content_type == "multipart/report" or msg.report_parts:
        reasons.append(R7_REPORT)

    # 8. 알려진 자동응답기 X-Mailer/User-Agent
    agents = [_fold(v) for v in (*_values(headers, "x-mailer"), *_values(headers, "user-agent"))]
    if any(pattern in agent for agent in agents for pattern in AUTORESPONDER_AGENT_PATTERNS):
        reasons.append(R8_AUTORESPONDER_AGENT)

    # 9. 제목 패턴
    subject = _fold(msg.subject or "")
    if any(pattern in subject for pattern in SUBJECT_PATTERNS):
        reasons.append(R9_SUBJECT_PATTERN)

    # 10. 내가 보낸 메일(모든 내 계정 주소·별칭) 또는 우리 앱의 자동발송 표식
    if (msg.from_addr_norm and msg.from_addr_norm in my_addresses) or headers.get(
        "x-emailtomcp-auto"
    ):
        reasons.append(R10_SELF_SENT)

    # 11. From이 정확히 1개가 아니거나(없음 포함), Sender가 있고 From과 다름(C2)
    if (
        msg.from_count != 1
        or not msg.from_addr_norm
        or (msg.sender_addr_norm is not None and msg.sender_addr_norm != msg.from_addr_norm)
    ):
        reasons.append(R11_FROM_SENDER)

    return reasons
