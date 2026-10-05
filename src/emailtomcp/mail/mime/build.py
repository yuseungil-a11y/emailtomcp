"""발신용 MIME 생성 (DESIGN.md §1.2 #23·#24, §6.3 create_reply_draft/create_forward_draft).

새 메일/회신/전체회신/전달(인라인·첨부) 네 종류를 만든다. 작성 형식은 P1 범위에서
평문(text/plain)이다(HTML 작성은 P4, §1.2 #12). 회신·전체회신은 원본 첨부파일을
기본으로 포함한다(`include_attachments=True`, 2026-10-04 사용자 지시, §1.2 #23) —
Claude 자동회신(auto_send)에는 이 빌더를 쓰지 않으므로 영향이 없다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from emailtomcp.mail.addressing import normalize_addr
from emailtomcp.mail.models import BuiltMessage, ParsedAddress, ParsedMessage

# Re:/Fwd:/RE:/FW:/회신:/전달: 류 접두어(반복 포함)를 제거하기 위한 패턴.
_REPLY_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd|회신|전달)\s*[:：]\s*", re.IGNORECASE)

_QUOTE_HEADER_FMT = "{date} {name} <{addr}> 작성:"


@dataclass(slots=True)
class OutgoingAttachment:
    """빌더에 넘기는 첨부 하나(원본 메일에서 그대로 가져온 것일 수도 있다)."""

    filename: str
    content_type: str | None
    payload: bytes
    content_id: str | None = None  # 인라인 전달 시에만 쓴다(cid 참조)


def normalize_subject(subject: str | None, prefix: str) -> str:
    """제목의 중복 Re:/Fwd: 접두어를 모두 제거하고 지정 접두어 하나만 앞에 붙인다."""
    base = subject or ""
    while True:
        stripped = _REPLY_PREFIX_RE.sub("", base, count=1)
        if stripped == base:
            break
        base = stripped
    base = base.strip()
    return f"{prefix} {base}" if base else prefix.strip()


def _format_from(addr: str, name: str | None) -> str:
    return f"{name} <{addr}>" if name else addr


def dedupe_addrs(addrs: list[str]) -> list[str]:
    """정규화 기준으로 중복 주소를 제거한다(표시용 원본 표기는 첫 등장을 유지)."""
    seen: set[str] = set()
    result: list[str] = []
    for addr in addrs:
        key = normalize_addr(addr)
        if key in seen:
            continue
        seen.add(key)
        result.append(addr)
    return result


def exclude_self(addrs: list[str], self_addr: str, aliases: list[str]) -> list[str]:
    """전체회신 수신자에서 내 주소와 별칭을 제외한다(§1.2 #23). `draft_service`도 재사용한다."""
    excluded = {normalize_addr(self_addr), *(normalize_addr(a) for a in aliases)}
    return [addr for addr in addrs if normalize_addr(addr) not in excluded]


def _quote_body(original: ParsedMessage, body_text: str) -> str:
    """회신 본문 아래에 원본을 `> ` 인용으로 덧붙인다."""
    sender = original.from_addrs[0] if original.from_addrs else None
    sender_name = (sender.name or sender.addr) if sender else ""
    sender_addr = sender.addr if sender else ""
    date_text = original.date.strftime("%Y-%m-%d %H:%M") if original.date else ""
    header = _QUOTE_HEADER_FMT.format(date=date_text, name=sender_name, addr=sender_addr)
    quoted_lines = [f"> {line}" for line in (original.body_text or "").splitlines()]
    quoted = "\n".join(quoted_lines)
    return f"{body_text}\n\n{header}\n{quoted}\n"


def _attach_part(msg: EmailMessage, attachment: OutgoingAttachment, *, inline: bool) -> None:
    maintype, _, subtype = (attachment.content_type or "application/octet-stream").partition("/")
    if not subtype:
        maintype, subtype = "application", "octet-stream"
    disposition = "inline" if inline else "attachment"
    msg.add_attachment(
        attachment.payload,
        maintype=maintype,
        subtype=subtype,
        filename=attachment.filename,
        disposition=disposition,
        cid=f"<{attachment.content_id}>" if inline and attachment.content_id else None,
    )


def _finalize(
    *,
    from_addr: str,
    from_name: str | None,
    to_addrs: list[str],
    cc_addrs: list[str],
    bcc_addrs: list[str],
    subject: str,
    body_text: str,
    attachments: list[OutgoingAttachment],
    in_reply_to: str | None,
    references: list[str],
) -> BuiltMessage:
    msg = EmailMessage()
    msg["From"] = _format_from(from_addr, from_name)
    if to_addrs:
        msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    # Bcc는 헤더로 남기지 않는다(수신자에게 노출되면 안 됨) — SMTP RCPT TO에서만 쓴다.
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)

    msg.set_content(body_text, charset="utf-8")

    for attachment in attachments:
        _attach_part(msg, attachment, inline=bool(attachment.content_id))

    return BuiltMessage(
        raw_bytes=msg.as_bytes(),
        message_id=message_id,
        subject=subject,
        body_text=body_text,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=bcc_addrs,
    )


def build_new(
    *,
    from_addr: str,
    from_name: str | None,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    cc_addrs: list[str] | None = None,
    bcc_addrs: list[str] | None = None,
    attachments: list[OutgoingAttachment] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> BuiltMessage:
    """새 메일(kind='new')을 만든다.

    `in_reply_to`/`references`는 기본적으로 새 메일에는 쓰지 않지만, `send_service`가
    이미 결정된 초안 필드(수신자·제목·본문)로 최종 MIME을 조립할 때 회신 스레딩
    헤더를 함께 넣을 수 있도록 선택적으로 받는다.
    """
    return _finalize(
        from_addr=from_addr,
        from_name=from_name,
        to_addrs=dedupe_addrs(to_addrs),
        cc_addrs=dedupe_addrs(cc_addrs or []),
        bcc_addrs=dedupe_addrs(bcc_addrs or []),
        subject=subject,
        body_text=body_text,
        attachments=attachments or [],
        in_reply_to=in_reply_to,
        references=references or [],
    )


def _reply_target(original: ParsedMessage) -> ParsedAddress | None:
    """회신 대상 주소. Reply-To가 있으면 그것을, 없으면 From을 쓴다(사용자 작성 UX 규칙,
    §7.5의 auto_send 전용 "From만" 규칙과는 별개다 — 이 빌더는 auto_send에 쓰지 않는다).
    """
    if original.reply_to_addrs:
        return original.reply_to_addrs[0]
    if original.from_addrs:
        return original.from_addrs[0]
    return None


def build_reply(
    original: ParsedMessage,
    *,
    from_addr: str,
    from_name: str | None,
    body_text: str,
    reply_all: bool = False,
    self_aliases: list[str] | None = None,
    include_attachments: bool = True,
    original_attachments: list[OutgoingAttachment] | None = None,
) -> BuiltMessage:
    """회신/전체회신(kind='reply'|'reply_all') 초안의 MIME을 만든다.

    원본 첨부파일을 기본으로 포함한다(`include_attachments=True`, §1.2 #23).
    """
    target = _reply_target(original)
    to_addrs = [target.addr] if target else []

    cc_addrs: list[str] = []
    if reply_all:
        aliases = self_aliases or []
        extra_to = [a.addr for a in original.to_addrs]
        to_addrs = dedupe_addrs([*to_addrs, *extra_to])
        to_addrs = exclude_self(to_addrs, from_addr, aliases)
        cc_addrs = dedupe_addrs([a.addr for a in original.cc_addrs])
        cc_addrs = exclude_self(cc_addrs, from_addr, aliases)

    subject = normalize_subject(original.subject, "Re:")
    quoted_body = _quote_body(original, body_text)

    references = [*original.references]
    if original.message_id and original.message_id not in references:
        references.append(original.message_id)

    attachments = list(original_attachments or []) if include_attachments else []

    return _finalize(
        from_addr=from_addr,
        from_name=from_name,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=[],
        subject=subject,
        body_text=quoted_body,
        attachments=attachments,
        in_reply_to=original.message_id,
        references=references,
    )


def build_forward_inline(
    original: ParsedMessage,
    *,
    from_addr: str,
    from_name: str | None,
    to_addrs: list[str],
    body_text: str,
    cc_addrs: list[str] | None = None,
    bcc_addrs: list[str] | None = None,
    original_attachments: list[OutgoingAttachment] | None = None,
) -> BuiltMessage:
    """전달(인라인, kind='forward_inline') — 원본 본문을 새 메일 본문 안에 그대로 포함한다."""
    sender = original.from_addrs[0] if original.from_addrs else None
    header_lines = [
        "---------- 전달된 메일 ----------",
        f"보낸사람: {sender.name or sender.addr}" if sender else "보낸사람: (알 수 없음)",
        f"날짜: {original.date.strftime('%Y-%m-%d %H:%M')}" if original.date else "",
        f"제목: {original.subject or ''}",
        f"받는사람: {', '.join(a.addr for a in original.to_addrs)}",
        "",
        original.body_text or "",
    ]
    forwarded_body = f"{body_text}\n\n" + "\n".join(
        line for line in header_lines if line is not None
    )

    subject = normalize_subject(original.subject, "Fwd:")

    return _finalize(
        from_addr=from_addr,
        from_name=from_name,
        to_addrs=dedupe_addrs(to_addrs),
        cc_addrs=dedupe_addrs(cc_addrs or []),
        bcc_addrs=dedupe_addrs(bcc_addrs or []),
        subject=subject,
        body_text=forwarded_body,
        attachments=list(original_attachments or []),
        in_reply_to=None,
        references=[*original.references, *([original.message_id] if original.message_id else [])],
    )


def build_forward_attach(
    original: ParsedMessage,
    original_raw: bytes,
    *,
    from_addr: str,
    from_name: str | None,
    to_addrs: list[str],
    body_text: str,
    cc_addrs: list[str] | None = None,
    bcc_addrs: list[str] | None = None,
) -> BuiltMessage:
    """전달(첨부로, kind='forward_attach') — 원본 `.eml`을 message/rfc822로 통째로 붙인다."""
    subject = normalize_subject(original.subject, "Fwd:")

    msg = EmailMessage()
    msg["From"] = _format_from(from_addr, from_name)
    to_addrs = dedupe_addrs(to_addrs)
    cc_addrs = dedupe_addrs(cc_addrs or [])
    if to_addrs:
        msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    message_id = make_msgid()
    msg["Message-ID"] = message_id

    msg.set_content(body_text, charset="utf-8")

    attach_filename = (
        f"{(original.subject or 'forwarded_message').strip() or 'forwarded_message'}.eml"
    )
    msg.add_attachment(
        original_raw,
        maintype="message",
        subtype="rfc822",
        filename=attach_filename,
    )

    return BuiltMessage(
        raw_bytes=msg.as_bytes(),
        message_id=message_id,
        subject=subject,
        body_text=body_text,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=[],
    )


__all__ = [
    "OutgoingAttachment",
    "build_forward_attach",
    "build_forward_inline",
    "build_new",
    "build_reply",
    "dedupe_addrs",
    "exclude_self",
    "normalize_subject",
]
