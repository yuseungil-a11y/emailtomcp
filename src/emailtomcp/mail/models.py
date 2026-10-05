"""mail 패키지 공용 도메인 모델 (DESIGN.md §4.1, §5.1 DDL과 느슨하게 대응).

`core/models.py`(상태값 enum)와는 별개다 — core는 이번 범위에서 건들지 않기로
했으므로, mail 전용 파싱/빌드 결과 모델은 이 모듈에 둔다. 의존 규칙(§4.2)상
mail은 core에만 의존해도 되지만, 이 모델들은 mail 내부에서만 쓰는 중간 산출물이라
core에 넣지 않았다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ParsedAddress:
    """RFC 5322 addr-spec 하나."""

    name: str | None
    addr: str  # 원본 그대로(addr-spec), 정규화는 호출자가 mail.addressing으로 한다


@dataclass(slots=True)
class ParsedAttachment:
    """첨부(또는 인라인 리소스) 메타 + 본문 바이트.

    `payload`는 DB에 저장하지 않는다 — 저장소 레이어가 안전 처리(attachments_safety)를
    거쳐 파일로 쓸 때만 쓴다.
    """

    part_index: str
    filename_raw: str | None
    content_type: str | None
    size_bytes: int
    is_inline: bool
    content_id: str | None
    payload: bytes = field(repr=False)


@dataclass(slots=True)
class ParsedMessage:
    """`.eml` bytes를 파싱한 결과(DESIGN.md §7.5, §8.4 H5, messages 테이블과 대응)."""

    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    subject: str | None
    from_addrs: list[ParsedAddress]  # 보통 1개. 2개 이상이면 LoopGuard 11(From 복수) 대상
    sender_addr: ParsedAddress | None
    to_addrs: list[ParsedAddress]
    cc_addrs: list[ParsedAddress]
    bcc_addrs: list[ParsedAddress]
    reply_to_addrs: list[ParsedAddress]
    date: datetime | None
    body_text: str  # 표시 파트(plain 또는 정화된 html에서 추출) = Claude 입력(H5)
    body_source: str | None  # "plain" | "html" | None
    has_html: bool
    content_mismatch: bool
    attachments: list[ParsedAttachment]
    size_bytes: int
    parse_limited: bool  # MIME 상한 초과로 일부만 파싱됐는지(§8.2 L5)
    body_truncated: bool  # DB 저장용 본문이 2MB 상한으로 잘렸는지
    # X-Priority/Importance → 1(높음)~5(낮음), 그리드 우선순위 표시용(§1.2 #25)
    priority: int | None = None


@dataclass(slots=True)
class BuiltMessage:
    """`mime/build.py`가 만든 발신용 MIME 결과.

    `body_text`는 인용/전달 머리글까지 합쳐진 최종 평문 본문이다 — `draft_service`가
    이 값을 그대로 `drafts.body_text`에 저장해, 이후 사용자가 다시 읽고 고칠 수 있게 한다
    (raw_bytes 자체는 drafts 테이블에 저장하지 않는다. 최종 발신 MIME은 send_service가
    편집된 drafts 필드로 다시 조립한다).
    """

    raw_bytes: bytes
    message_id: str
    subject: str
    body_text: str
    to_addrs: list[str]
    cc_addrs: list[str]
    bcc_addrs: list[str]
