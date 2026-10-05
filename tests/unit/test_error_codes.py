"""`core.error_codes.ErrorCode` 레지스트리 회귀 테스트.

- 코드 문자열(.value) 중복 금지: `StrEnum`은 값이 같은 멤버를 조용히 별칭(alias)
  처리해 버리므로, Python 자체가 중복을 막아 주지 않는다 — 여기서 직접 검사한다.
- 모든 멤버에 한국어 설명이 등록돼 있는지 확인한다.
"""

from __future__ import annotations

from emailtomcp.core.error_codes import ErrorCode, describe


def test_error_code_values_are_unique() -> None:
    # StrEnum.__members__는 별칭(alias)도 포함하므로, 순회(iteration)로는 가려지는
    # 값 중복을 여기서 잡을 수 있다(순회 기반 검사는 중복을 조용히 놓친다).
    values = [member.value for member in ErrorCode.__members__.values()]
    assert len(values) == len(set(values)), "ErrorCode 값(코드 문자열)이 중복되면 안 된다"


def test_error_code_members_match_alias_free_count() -> None:
    # StrEnum은 값이 같으면 별칭이 되어 __members__에는 보이지만 순회(iteration)에는
    # 빠진다 — 즉 아래가 깨지면 "중복 때문에 조용히 사라진 멤버가 있다"는 뜻이다.
    assert len(list(ErrorCode)) == len(ErrorCode.__members__)


def test_all_codes_follow_emcp_prefix_format() -> None:
    for member in ErrorCode:
        assert member.value.startswith("EMCP-"), member.value
        suffix = member.value.removeprefix("EMCP-")
        assert suffix.isdigit() and len(suffix) == 4, member.value


def test_describe_returns_korean_text_for_every_member() -> None:
    for member in ErrorCode:
        text = describe(member)
        assert text, member
        assert "알 수 없는" not in text
        assert "설명이 등록되지 않은" not in text


def test_describe_accepts_raw_code_string() -> None:
    assert describe("EMCP-1002") == describe(ErrorCode.IMAP_AUTH_FAILED)


def test_describe_handles_unknown_code_gracefully() -> None:
    text = describe("EMCP-0000")
    assert "알 수 없는" in text
