"""thread_key 산출 (DESIGN.md §5.5 C-09).

1. 루트 ID: References의 첫 Message-ID → 없으면 In-Reply-To → 없으면 자신의 Message-ID.
2. 셋 다 없으면 `정규화 제목(Re:/Fwd: 반복 제거) + 참여자 집합`의 해시를 쓴다.
3. `thread_key = sha256(account_id + ":" + root)[:32]`.

thread_key는 **UI 묶음 표시 전용**이다 — Claude에게 스레드를 줄 때는 별도로 참여자
검증을 거쳐야 한다(H1, 이 모듈의 책임 밖).
"""

from __future__ import annotations

import hashlib
import re

from emailtomcp.mail.models import ParsedMessage

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd|회신|전달)\s*[:：]\s*", re.IGNORECASE)


def _normalize_subject(subject: str) -> str:
    base = subject
    while True:
        stripped = _SUBJECT_PREFIX_RE.sub("", base, count=1)
        if stripped == base:
            break
        base = stripped
    return base.strip().lower()


def compute_thread_key(account_id: int, parsed: ParsedMessage) -> str:
    root: str | None = None
    if parsed.references:
        root = parsed.references[0]
    elif parsed.in_reply_to:
        root = parsed.in_reply_to
    elif parsed.message_id:
        root = parsed.message_id

    if root:
        basis = root
    else:
        normalized_subject = _normalize_subject(parsed.subject or "")
        participants = sorted(
            {
                a.addr.lower()
                for a in (*parsed.from_addrs, *parsed.to_addrs, *parsed.cc_addrs)
                if a.addr
            }
        )
        basis = normalized_subject + "|" + ",".join(participants)

    digest = hashlib.sha256(f"{account_id}:{basis}".encode()).hexdigest()
    return digest[:32]
