"""`mail.threading.compute_thread_key` 단위 테스트 (DESIGN.md §5.5 C-09).

루트 ID 선택 우선순위(References > In-Reply-To > 자신의 Message-ID > 제목+참여자 해시)와
제목 접두사(Re:/Fwd:/회신:/전달:) 정규화를 확인한다.
"""

from __future__ import annotations

import pytest

from emailtomcp.mail.models import ParsedAddress, ParsedMessage
from emailtomcp.mail.threading import compute_thread_key

pytestmark = pytest.mark.unit


def _msg(**kw: object) -> ParsedMessage:
    base: dict[str, object] = {
        "message_id": None,
        "in_reply_to": None,
        "references": [],
        "subject": None,
        "from_addrs": [],
        "sender_addr": None,
        "to_addrs": [],
        "cc_addrs": [],
        "bcc_addrs": [],
        "reply_to_addrs": [],
        "date": None,
        "body_text": "",
        "body_source": None,
        "has_html": False,
        "content_mismatch": False,
        "attachments": [],
        "size_bytes": 0,
        "parse_limited": False,
        "body_truncated": False,
    }
    base.update(kw)
    return ParsedMessage(**base)  # type: ignore[arg-type]


def test_root_prefers_references_over_in_reply_to_and_message_id() -> None:
    by_ref = compute_thread_key(
        1, _msg(references=["<root@x>"], in_reply_to="<other@x>", message_id="<self@x>")
    )
    by_ref2 = compute_thread_key(1, _msg(references=["<root@x>", "<mid@x>"]))
    assert by_ref == by_ref2  # References의 "첫" ID만 쓴다

    # References가 없을 때만 In-Reply-To를 쓴다 — 같은 root면 같은 키가 나와야 한다.
    by_reply_same_root = compute_thread_key(1, _msg(in_reply_to="<root@x>"))
    assert by_reply_same_root == by_ref

    # root 자체가 다르면 다른 키가 나온다.
    by_reply_other_root = compute_thread_key(1, _msg(in_reply_to="<different@x>"))
    assert by_reply_other_root != by_ref


def test_root_falls_back_to_own_message_id_then_subject_participants() -> None:
    own_id = compute_thread_key(1, _msg(message_id="<self@x>"))
    assert len(own_id) == 32

    none_at_all = compute_thread_key(
        1,
        _msg(
            subject="문의사항",
            from_addrs=[ParsedAddress(name=None, addr="a@x.example")],
            to_addrs=[ParsedAddress(name=None, addr="b@x.example")],
        ),
    )
    assert len(none_at_all) == 32
    assert none_at_all != own_id


def test_subject_prefix_normalization_groups_same_thread() -> None:
    """Re:/Fwd:/회신:/전달: 접두사(반복 포함)는 제거되고, 대소문자·공백도 무시한다."""
    participants = [ParsedAddress(name=None, addr="a@x.example")]

    base = compute_thread_key(1, _msg(subject="견적 요청", from_addrs=participants))
    re_prefixed = compute_thread_key(1, _msg(subject="RE: 견적 요청", from_addrs=participants))
    repeated = compute_thread_key(
        1, _msg(subject="회신: Re: 전달: 견적 요청", from_addrs=participants)
    )
    spaced = compute_thread_key(1, _msg(subject="  re : 견적 요청  ", from_addrs=participants))

    assert base == re_prefixed == repeated == spaced


def test_different_account_id_yields_different_key_for_same_message() -> None:
    msg = _msg(message_id="<self@x>")
    assert compute_thread_key(1, msg) != compute_thread_key(2, msg)


def test_no_root_and_no_subject_uses_empty_basis_without_crashing() -> None:
    key = compute_thread_key(1, _msg())
    assert len(key) == 32
