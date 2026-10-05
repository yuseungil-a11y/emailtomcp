"""LoopGuard 판정용 헤더 원자료 추출 (DESIGN.md §7.3, §7.9 G1) — P3 Phase B.

수신 저장(SyncService, G1) 시점에 원문 `.eml`에서 LoopGuard 정적 조건(1~11)에 필요한 헤더만
뽑아 `messages.auto_headers`(JSON)에 기록한다. **판정은 하지 않는다**(판정은 rules/loop_guard.py).

기록 형식(v1)
```
{"v": 1,
 "is_backfill": bool,                      # 계정 최초 동기화로 들어온 메일(§7.2 1단계)
 "headers": {"auto-submitted": ["..."], ...},  # 아래 _HEADER_NAMES만, 이름은 소문자
 "content_type": "multipart/report",       # 최상위 Content-Type(소문자, 매개변수 제외)
 "report_parts": bool}                     # message/delivery-status|disposition-notification 파트
```

- 값마다 512자, 헤더 이름마다 20개로 자른다(헤더 폭탄 방지). 자른 결과로 차단 판정이 바뀌는
  방향은 "덜 차단"이 아니라 그대로다 — 차단 조건은 존재 여부·접두 일치로만 본다.
- 이 JSON이 없거나 깨져 있으면 RuleEngine은 그 메일을 평가하지 않는다(fail-closed).

AR(Authentication-Results)·Received 원자료 기록(§7.9 G1의 나머지)은 AuthGate가 들어오는
Phase C에서 추가한다.
"""

from __future__ import annotations

import email.policy
import json
import re
from email.parser import BytesHeaderParser
from typing import Any

LOOP_HEADERS_VERSION = 1

_HEADER_NAMES: tuple[str, ...] = (
    "auto-submitted",
    "precedence",
    "list-id",
    "list-unsubscribe",
    "list-post",
    "feedback-id",
    "x-auto-response-suppress",
    "x-autoreply",
    "x-autorespond",
    "x-auto-reply",
    "x-autoreply-from",
    "x-mail-autoreply",
    "return-path",
    "x-mailer",
    "user-agent",
    "x-emailtomcp-auto",
    "from",
    "sender",
    "content-type",
)
_MAX_VALUE_CHARS = 512
_MAX_VALUES_PER_NAME = 20
# 접힌(folded) Content-Type 줄도 잡는다. 오탐은 "차단" 쪽이라 안전하다.
_REPORT_PART_RE = re.compile(
    rb"(?im)^content-type:[ \t]*(?:\r?\n[ \t]+)?"
    rb"message/(?:delivery-status|disposition-notification)"
)


def _clean(value: object) -> str:
    text = str(value)
    # 헤더 값의 개행·제어문자를 공백으로 바꾼다(저장·비교용 평문).
    text = "".join(ch if ch >= " " or ch == "\t" else " " for ch in text)
    return text.strip()[:_MAX_VALUE_CHARS]


def extract_loop_headers(raw: bytes, *, is_backfill: bool) -> dict[str, Any]:
    """원문에서 LoopGuard 원자료를 뽑는다. 헤더 블록만 파싱한다(본문은 정규식 1회 검색)."""
    headers: dict[str, list[str]] = {}
    content_type = ""
    try:
        msg = BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw)
        for name in _HEADER_NAMES:
            values = msg.get_all(name) or []
            if values:
                headers[name] = [_clean(v) for v in values[:_MAX_VALUES_PER_NAME]]
        raw_ct = msg.get("content-type")
        if raw_ct:
            content_type = _clean(raw_ct).split(";", 1)[0].strip().lower()
    except Exception:  # noqa: BLE001 — 파싱 실패는 아래 parse_error로 표시(판정 쪽에서 차단)
        return {
            "v": LOOP_HEADERS_VERSION,
            "is_backfill": bool(is_backfill),
            "headers": {},
            "content_type": "",
            "report_parts": False,
            "parse_error": True,
        }
    return {
        "v": LOOP_HEADERS_VERSION,
        "is_backfill": bool(is_backfill),
        "headers": headers,
        "content_type": content_type,
        "report_parts": bool(_REPORT_PART_RE.search(raw)),
    }


def dumps_loop_headers(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def loads_loop_headers(raw: str | None) -> dict[str, Any] | None:
    """저장된 JSON을 읽는다. 없거나 형식이 다르면 None(호출자가 fail-closed 처리)."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("v") != LOOP_HEADERS_VERSION:
        return None
    if not isinstance(value.get("headers"), dict) or not isinstance(value.get("is_backfill"), bool):
        return None
    return value
