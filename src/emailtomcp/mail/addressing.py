"""주소(addr-spec) 정규화 (DESIGN.md §7.5 C2/C-15, §6.3 전체회신 제외 판단의 공통 근거).

- 정규화 비교는 addr-spec 기준이다. 표시이름은 무시한다.
- 도메인은 소문자 + IDNA(punycode)로 정규화한다.
- 로컬파트는 대소문자를 무시한다(비교·매칭 용도로만. 사용자에게 보여줄 원본은 그대로 둔다).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SplitAddr:
    local: str
    domain: str


def split_addr(addr: str) -> SplitAddr | None:
    """`local@domain`으로 나눈다. `@`가 없거나 빈 문자열이면 None."""
    addr = addr.strip()
    if "@" not in addr:
        return None
    local, _, domain = addr.rpartition("@")
    if not local or not domain:
        return None
    return SplitAddr(local=local, domain=domain)


def normalize_domain(domain: str) -> str:
    """도메인을 소문자 + IDNA(punycode)로 정규화한다. 실패하면 소문자만 적용한다."""
    lowered = domain.strip().lower().rstrip(".")
    try:
        return lowered.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return lowered


def normalize_addr(addr: str) -> str:
    """비교/매칭 전용 정규화 addr-spec(`local@domain`, 모두 소문자)을 만든다.

    사용자에게 보여줄 원본 표시용 주소는 그대로 쓰고, 이 함수의 결과는
    `*_addr_norm`/`draft_recipients.addr_norm` 같은 매칭 컬럼에만 쓴다.
    """
    parts = split_addr(addr)
    if parts is None:
        return addr.strip().lower()
    return f"{parts.local.lower()}@{normalize_domain(parts.domain)}"


def addr_domain_norm(addr: str) -> str:
    """매칭용 도메인 정규화 결과만 돌려준다(`draft_recipients.domain_norm`)."""
    parts = split_addr(addr)
    if parts is None:
        return ""
    return normalize_domain(parts.domain)


def addrs_equal(a: str, b: str) -> bool:
    """addr-spec 두 개가 매칭 정규화 기준으로 같은지 비교한다(C-15)."""
    return normalize_addr(a) == normalize_addr(b)
