"""`.eml` bytes → 도메인 모델 파싱 (DESIGN.md §7.5, §8.2 L5, §8.4 H5, §9.4).

- RFC 2231과 비표준 encoded-word 첨부파일명을 모두 처리한다.
- 화면에 표시할 본문(body_text)과 Claude에 줄 입력이 같아야 한다는 H5 원칙에 따라,
  HTML 파트가 있으면 `html_sanitize.sanitize_html`로 정화한 뒤 그 결과에서 텍스트를
  뽑아 `body_text`로 쓴다(원본 HTML은 별도로 저장하지 않는다).
- plain/html 두 파트가 모두 있으면 정규화 텍스트의 Jaccard 유사도로 `content_mismatch`를
  판단한다(기준 0.6, §8.4).
- MIME 상한(§8.2)을 넘으면 예외를 던지지 않고 `parse_limited=True`로 안전하게 강등한다.
"""

from __future__ import annotations

import email
import email.header
import email.message
import email.utils
import logging
import re
import unicodedata
from datetime import datetime

import html2text

from emailtomcp.mail.html_sanitize import sanitize_html
from emailtomcp.mail.mime import limits as mime_limits
from emailtomcp.mail.mime.charset import decode_bytes
from emailtomcp.mail.models import ParsedAddress, ParsedAttachment, ParsedMessage

logger = logging.getLogger(__name__)

CONTENT_MISMATCH_THRESHOLD = 0.6

_FOLD_RE = re.compile(r"\r\n(?=[ \t])|\n(?=[ \t])")
_MSGID_RE = re.compile(r"<[^<>\s]+>")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def parse_eml(raw: bytes) -> ParsedMessage:
    """`.eml` bytes를 `ParsedMessage`로 바꾼다. 어떤 입력에도 예외를 던지지 않는다."""
    size = len(raw)
    try:
        mime_limits.check_raw_size(size)
        _check_header_block(raw)
        msg = email.message_from_bytes(raw)
        return _parse_message(msg, size_bytes=size)
    except mime_limits.LimitExceeded as exc:
        logger.warning("MIME 상한 초과로 파싱 제한 모드로 전환: %s", exc.reason)
        return _limited_result(raw, size)
    except Exception:  # noqa: BLE001 — 파싱 실패도 "파싱 제한"으로 안전하게 강등한다
        logger.exception("MIME 파싱 실패, 파싱 제한 모드로 강등")
        return _limited_result(raw, size)


# --- 헤더 상한 선검사 ------------------------------------------------------


def _check_header_block(raw: bytes) -> None:
    header_block = raw
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = raw.find(sep)
        if idx != -1:
            header_block = raw[:idx]
            break
    mime_limits.check_header_block_size(len(header_block))

    current = bytearray()
    for line in header_block.splitlines(keepends=True):
        if line[:1] in (b" ", b"\t") and current:
            current.extend(line)
            continue
        if current:
            mime_limits.check_single_header_size(len(current))
        current = bytearray(line)
    if current:
        mime_limits.check_single_header_size(len(current))


# --- 헤더 디코딩 -------------------------------------------------------------


def _unfold(value: str | email.header.Header) -> str:
    """헤더 폴딩(CRLF + WSP)의 줄바꿈만 제거한다. 공백/탭은 그대로 남긴다.

    `email.message.Message.get()`은 RFC 2047 인코딩 없이 비ASCII 바이트가 그대로
    담긴 헤더(RFC 6532 SMTPUTF8, 일부 국내 레거시 발송 시스템에서 실제 발생)를
    `str`이 아니라 `email.header.Header` 객체로 돌려준다. 그대로 정규식에 넘기면
    `TypeError`가 나서 메일 전체가 `parse_limited`로 강등되므로, 먼저
    `_decode_header_text`와 같은 경로로 디코딩해 문자열로 바꾼 뒤 폴딩을 제거한다.
    """
    if not isinstance(value, str):
        value = _decode_header_text(value) or ""
    return _FOLD_RE.sub("", value)


def _decode_header_text(value: str | email.header.Header | None) -> str | None:
    """RFC 2047 encoded-word와 비표준 변형을 모두 우리 charset 디코더로 처리한다.

    `value`가 `email.header.Header`(비ASCII 8비트 헤더, RFC 6532)인 경우도
    `email.header.decode_header`가 내부 `_chunks`를 그대로 넘겨주므로 안전하게
    처리된다(인코딩 라벨은 `unknown-8bit`로 떨어지고 `decode_bytes`가 추정한다).
    """
    if not value:
        return None
    try:
        parts = email.header.decode_header(value)
    except Exception:  # noqa: BLE001 — 비표준 헤더도 원문 그대로 폴백
        # value가 Header 객체일 수도 있어 반드시 str로 반환 계약을 지킨다
        # (그대로 돌려주면 호출부 _unfold의 정규식이 다시 TypeError를 낸다).
        return value if isinstance(value, str) else str(value)
    chunks: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            chunks.append(decode_bytes(chunk, charset))
        else:
            chunks.append(chunk)
    return "".join(chunks)


def _parse_address_list(raw_value: str | email.header.Header | None) -> list[ParsedAddress]:
    if not raw_value:
        return []
    pairs = email.utils.getaddresses([_unfold(raw_value)])
    result: list[ParsedAddress] = []
    for name, addr in pairs:
        if not addr:
            continue
        decoded_name = _decode_header_text(name) if name else None
        result.append(ParsedAddress(name=decoded_name or None, addr=addr))
    return result


def _first_msgid(raw_value: str | email.header.Header | None) -> str | None:
    if not raw_value:
        return None
    value = _unfold(raw_value).strip()
    if not value:
        return None
    match = _MSGID_RE.search(value)
    return match.group(0) if match else value


def _split_msgids(raw_value: str | email.header.Header | None) -> list[str]:
    if not raw_value:
        return []
    value = _unfold(raw_value)
    found = _MSGID_RE.findall(value)
    return found if found else value.split()


def _parse_date(raw_value: str | email.header.Header | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        return email.utils.parsedate_to_datetime(_unfold(raw_value))
    except (TypeError, ValueError, OverflowError):
        return None


_IMPORTANCE_TO_PRIORITY: dict[str, int] = {"high": 1, "normal": 3, "low": 5}


def _parse_priority(
    x_priority: str | email.header.Header | None,
    importance: str | email.header.Header | None,
) -> int | None:
    """`X-Priority`(1~5) 또는 `Importance`(high/normal/low) → 1(높음)~5(낮음) 정수(§1.2 #25)."""
    if x_priority:
        try:
            value = int(_unfold(x_priority).strip().split()[0])
        except (ValueError, IndexError):
            value = None
        if value is not None and 1 <= value <= 5:
            return value
    if importance:
        return _IMPORTANCE_TO_PRIORITY.get(_unfold(importance).strip().lower())
    return None


def _filename_param_from_raw_header(
    part: email.message.Message, header_name: str, param_name: str
) -> str | None:
    """`header_name`이 비ASCII 8비트 바이트를 RFC 2047/2231 인코딩 없이 그대로
    담은 `email.header.Header` 객체일 때, `Message.get_filename()`을 거치지 않고
    직접 `param_name` 파라미터 값을 꺼낸다.

    `Message.get_filename()`은 내부적으로 `str(Header)`를 거치는데, compat32
    정책이 ``surrogateescape``로 들여온 8비트 바이트를 `str(Header)`가 다시
    유효한 유니코드로 인코딩하려 하면서 복구 불가능한 ``U+FFFD``로 뭉개버린다
    (QA D-2). 대신 원본 `Header` 객체를 `_decode_header_text`로 먼저 안전하게
    디코딩한 뒤, 그 결과 문자열로 임시 헤더를 만들어 표준 파라미터 파서
    (RFC 2231 continuation 등)를 재사용한다.

    헤더가 이미 `str`(순수 ASCII)이면 `Message.get_filename()`의 기존 경로가
    안전하므로 `None`을 돌려주어 호출부가 그 경로를 쓰도록 한다.
    """
    raw_header = part.get(header_name)
    if raw_header is None or isinstance(raw_header, str):
        return None
    decoded_header = _decode_header_text(raw_header)
    if not decoded_header:
        return None
    probe = email.message.Message()
    probe[header_name] = decoded_header
    try:
        filename = probe.get_param(param_name, None, header_name.lower())
    except Exception:  # noqa: BLE001 — 깨진 파라미터도 무시하고 다음 경로로
        return None
    if not filename:
        return None
    try:
        return email.utils.collapse_rfc2231_value(filename).strip()
    except Exception:  # noqa: BLE001
        return str(filename).strip()


def _attachment_filename(part: email.message.Message) -> str | None:
    disposition_filename = _filename_param_from_raw_header(
        part, "Content-Disposition", "filename"
    )
    if disposition_filename:
        return disposition_filename
    # `_filename_param_from_raw_header`는 Content-Disposition이 순수 ASCII(str)일 때
    # 일부러 None을 돌려주고 아래 `get_filename()` 경로를 타게 한다(D-2). 하지만 그
    # 경로를 타기 전에 Content-Type의 8비트 name을 먼저 확인해 버리면, ASCII인
    # Content-Disposition filename이 있어도 Content-Type 쪽이 선택되어 `get_filename()`
    # 과 반대 우선순위가 된다(R-1, `Message.get_filename()`은 Content-Disposition을
    # 항상 먼저 본다). Content-Type 헬퍼를 부르기 전에 ASCII Content-Disposition의
    # filename 파라미터를 먼저 확인한다.
    raw_disposition = part.get("Content-Disposition")
    if isinstance(raw_disposition, str):
        try:
            ascii_filename = part.get_param("filename", None, "content-disposition")
        except Exception:  # noqa: BLE001 — 깨진 파라미터도 무시하고 다음 경로로
            ascii_filename = None
        if ascii_filename:
            try:
                collapsed = email.utils.collapse_rfc2231_value(ascii_filename).strip()
            except Exception:  # noqa: BLE001
                collapsed = str(ascii_filename).strip()
            # 값 자체가 RFC 2047 encoded-word를 비표준으로 filename= 안에 바로 적은
            # 경우(get_filename() 최종 경로와 동일하게 디코딩해 준다. 예: 구형 한국
            # 메일 클라이언트가 보내는 `filename="=?EUC-KR?B?...?="`).
            decoded = _decode_header_text(collapsed)
            return decoded or collapsed
    contenttype_filename = _filename_param_from_raw_header(part, "Content-Type", "name")
    if contenttype_filename:
        return contenttype_filename

    try:
        raw = part.get_filename()
    except Exception:  # noqa: BLE001 — 깨진 RFC 2231 파라미터도 무시하고 진행
        raw = None
    if raw is None:
        return None
    decoded = _decode_header_text(_unfold(raw))
    return decoded or raw


# --- 본문/첨부 추출 ----------------------------------------------------------


def _walk_parts(msg: email.message.Message):
    """(part, part_index, depth)를 순서대로 낸다. part_index는 "1.2.1" 형태."""

    def _walk(part: email.message.Message, prefix: str, depth: int):
        yield part, prefix, depth
        if part.is_multipart():
            for i, sub in enumerate(part.get_payload(), start=1):
                yield from _walk(sub, f"{prefix}.{i}", depth + 1)

    yield from _walk(msg, "1", 0)


def _html_to_text(html: str) -> str:
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    return converter.handle(html).strip()


def _tokenize(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return set(_TOKEN_RE.findall(normalized))


def _jaccard_similarity(a: str, b: str) -> float:
    set_a, set_b = _tokenize(a), _tokenize(b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    union = len(set_a | set_b)
    if union == 0:
        return 1.0
    return len(set_a & set_b) / union


def _resolve_body(
    plain_text: str | None, html_text: str | None
) -> tuple[str, str | None, bool, bool]:
    has_html = html_text is not None
    sanitized_html_text: str | None = None
    if html_text is not None:
        sanitized_html_text = _html_to_text(sanitize_html(html_text))

    if sanitized_html_text is not None:
        body_text, body_source = sanitized_html_text, "html"
    elif plain_text is not None:
        body_text, body_source = plain_text, "plain"
    else:
        body_text, body_source = "", None

    content_mismatch = False
    if plain_text is not None and sanitized_html_text is not None:
        similarity = _jaccard_similarity(plain_text, sanitized_html_text)
        content_mismatch = similarity < CONTENT_MISMATCH_THRESHOLD

    return body_text, body_source, has_html, content_mismatch


def _parse_message(msg: email.message.Message, *, size_bytes: int) -> ParsedMessage:
    message_id = _first_msgid(msg.get("Message-ID"))
    in_reply_to = _first_msgid(msg.get("In-Reply-To"))
    references = _split_msgids(msg.get("References"))
    subject = _decode_header_text(msg.get("Subject"))
    from_addrs = _parse_address_list(msg.get("From"))
    sender_list = _parse_address_list(msg.get("Sender"))
    sender_addr = sender_list[0] if sender_list else None
    to_addrs = _parse_address_list(msg.get("To"))
    cc_addrs = _parse_address_list(msg.get("Cc"))
    bcc_addrs = _parse_address_list(msg.get("Bcc"))
    reply_to_addrs = _parse_address_list(msg.get("Reply-To"))
    date = _parse_date(msg.get("Date"))
    priority = _parse_priority(msg.get("X-Priority"), msg.get("Importance"))

    plain_text: str | None = None
    html_text: str | None = None
    attachments: list[ParsedAttachment] = []
    part_count = 0
    decoded_total = 0
    parse_limited = False

    try:
        for part, part_index, depth in _walk_parts(msg):
            part_count += 1
            mime_limits.check_part_count(part_count)
            mime_limits.check_nesting_depth(depth)

            if part.get_content_maintype() == "multipart":
                continue

            content_type = part.get_content_type()
            disposition = _unfold(part.get("Content-Disposition") or "").lower()
            filename = _attachment_filename(part)
            is_attachment_like = "attachment" in disposition or filename is not None

            payload = part.get_payload(decode=True) or b""
            decoded_total += len(payload)
            mime_limits.check_decoded_total(decoded_total)

            if is_attachment_like or content_type not in ("text/plain", "text/html"):
                content_id = part.get("Content-ID")
                if content_id:
                    content_id = _unfold(content_id).strip().strip("<>")
                attachments.append(
                    ParsedAttachment(
                        part_index=part_index,
                        filename_raw=filename,
                        content_type=content_type,
                        size_bytes=len(payload),
                        is_inline=bool(content_id) and "attachment" not in disposition,
                        content_id=content_id or None,
                        payload=payload,
                    )
                )
                continue

            charset = part.get_content_charset()
            text = decode_bytes(payload, charset)
            if content_type == "text/plain" and plain_text is None:
                plain_text = text
            elif content_type == "text/html" and html_text is None:
                html_text = text
    except mime_limits.LimitExceeded as exc:
        logger.warning("MIME 상한 초과(본문/첨부 처리 중): %s", exc.reason)
        parse_limited = True

    body_text, body_source, has_html, content_mismatch = _resolve_body(plain_text, html_text)
    body_text, truncated = mime_limits.clamp_db_body(body_text)

    return ParsedMessage(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        subject=subject,
        from_addrs=from_addrs,
        sender_addr=sender_addr,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=bcc_addrs,
        reply_to_addrs=reply_to_addrs,
        date=date,
        body_text=body_text,
        body_source=body_source,
        has_html=has_html,
        content_mismatch=content_mismatch,
        attachments=attachments,
        size_bytes=size_bytes,
        parse_limited=parse_limited,
        body_truncated=truncated,
        priority=priority,
    )


def _limited_result(raw: bytes, size: int) -> ParsedMessage:
    """상한 초과·파싱 실패 시 최소한의 헤더 요약만 담아 안전하게 반환한다."""
    subject: str | None = None
    from_addrs: list[ParsedAddress] = []
    date: datetime | None = None
    message_id: str | None = None
    try:
        header_block = raw
        for sep in (b"\r\n\r\n", b"\n\n"):
            idx = raw.find(sep)
            if idx != -1:
                header_block = raw[:idx]
                break
        header_block = header_block[: mime_limits.MAX_HEADERS_TOTAL_BYTES]
        msg = email.message_from_bytes(header_block)
        subject = _decode_header_text(msg.get("Subject"))
        from_addrs = _parse_address_list(msg.get("From"))
        date = _parse_date(msg.get("Date"))
        message_id = _first_msgid(msg.get("Message-ID"))
    except Exception:  # noqa: BLE001 — 헤더 요약조차 실패하면 완전히 빈 모델을 돌려준다
        logger.debug("파싱 제한 모드 헤더 요약 추출도 실패")

    return ParsedMessage(
        message_id=message_id,
        in_reply_to=None,
        references=[],
        subject=subject,
        from_addrs=from_addrs,
        sender_addr=None,
        to_addrs=[],
        cc_addrs=[],
        bcc_addrs=[],
        reply_to_addrs=[],
        date=date,
        body_text="",
        body_source=None,
        has_html=False,
        content_mismatch=False,
        attachments=[],
        size_bytes=size,
        parse_limited=True,
        body_truncated=False,
        priority=None,
    )
