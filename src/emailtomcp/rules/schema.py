"""규칙 조건 스키마와 저장 검증 (DESIGN.md §7.1, §7.6 A5, §11.4 G0) — P3 Phase B.

조건 JSON 형식(rules.conditions_json)
```
{"match": "all" | "any",
 "conditions": [{"field": "...", "op": "...", "value": ...}, ...]}
```
- 조건이 비어 있으면 "모든 메일"에 맞는다(편집 창이 경고한다).
- field/op 조합과 값 형식은 `_FIELD_OPS`/`_validate_condition`이 강제한다.
- regex는 RE2(regex_safe), 200자 이하, 저장 시 컴파일 검증.
- between은 반열림 구간 `s ≤ x < e`, `s > e`이면 자정(주말)을 넘는 구간, `s == e`는 오류(C-06).
- `auth` 필드(dmarc_pass/dkim_aligned_pass)는 형식만 받는다. AuthGate(§7.6)는 Phase C에서
  들어오므로 **이번 단계 평가에서는 항상 불일치**로 본다(fail-closed, rules/engine.py).

저장 검증(`validate_rule`)
- Phase B에서는 action으로 `ignore`/`draft`만 저장할 수 있다(`allow_auto_send=False`). 즉시발송
  (auto_send)은 다음 업데이트에서 연다 — 편집 창도 그 항목을 비활성으로 보여 준다.
- auto_send 저장 경로(Phase C용)는 A5(발신자 화이트리스트 필수, match=all)와 무료 메일 도메인
  경고까지 지금 구현해 둔다.
- 고정 템플릿은 허용 슬롯(`{sender_name_safe}`, `{received_date}`, `{account_name}`)만 쓸 수 있다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from emailtomcp.rules.regex_safe import RegexError, compile_rule_regex

MATCH_MODES = ("all", "any")
ACTIONS_ALL = ("ignore", "draft", "auto_send")
ACTIONS_PHASE_B = ("ignore", "draft")
REPLY_MODES = ("generated", "fixed_template")
AUTOSEND_NOT_YET_MESSAGE = "즉시발송(auto_send)은 다음 업데이트에서 제공될 예정입니다"

_TEXT_OPS = ("equals", "contains", "not_contains", "starts_with", "ends_with", "regex")
_ADDR_OPS = (*_TEXT_OPS, "in_list")

_FIELD_OPS: dict[str, tuple[str, ...]] = {
    "from_address": _ADDR_OPS,
    "from_domain": _ADDR_OPS,
    "to": _ADDR_OPS,
    "cc": _ADDR_OPS,
    "subject": _TEXT_OPS,
    "body": _TEXT_OPS,
    "has_attachment": ("equals",),
    "received_hour": ("equals", "between"),
    "received_weekday": ("equals", "between"),
    "auth": ("equals",),
}
FIELDS: tuple[str, ...] = tuple(_FIELD_OPS)
AUTH_VALUES = ("dmarc_pass", "dkim_aligned_pass")

MAX_CONDITIONS = 30
MAX_VALUE_CHARS = 500
MAX_LIST_ITEMS = 200
MAX_LIST_ITEM_CHARS = 254
MAX_NAME_CHARS = 100
MAX_TEMPLATE_CHARS = 2000
MAX_INSTRUCTIONS_CHARS = 1000

TEMPLATE_SLOTS = ("sender_name_safe", "received_date", "account_name")
_SLOT_RE = re.compile(r"\{([^{}]*)\}")

# 무료 메일 도메인(A5 경고용, §7.6). 저장은 막지 않는다.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "naver.com",
        "daum.net",
        "hanmail.net",
        "kakao.com",
        "nate.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
    }
)


class RuleSchemaError(ValueError):
    """규칙 저장 검증 실패(사용자에게 그대로 보여 줄 한국어 메시지)."""


@dataclass(frozen=True, slots=True)
class Condition:
    field: str
    op: str
    value: Any
    compiled: Any = None  # regex일 때 RE2 객체


@dataclass(frozen=True, slots=True)
class ConditionSet:
    match: str
    conditions: tuple[Condition, ...]


@dataclass(slots=True)
class ValidatedRule:
    """repo에 넘길 값(`fields`)과 사용자에게 보여 줄 경고."""

    fields: dict[str, Any]
    enabled: bool
    warnings: list[str] = field(default_factory=list)


def _check_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleSchemaError(f"{label}: 값이 비어 있습니다")
    if len(value) > MAX_VALUE_CHARS:
        raise RuleSchemaError(f"{label}: 값은 {MAX_VALUE_CHARS}자 이하여야 합니다")
    return value


def _check_range(value: Any, label: str, *, low: int, high: int) -> list[int]:
    if (
        not isinstance(value, list | tuple)
        or len(value) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    ):
        raise RuleSchemaError(f"{label}: between 값은 [시작, 끝] 두 정수여야 합니다")
    start, end = int(value[0]), int(value[1])
    if not (low <= start <= high and low <= end <= high):
        raise RuleSchemaError(f"{label}: 값은 {low}~{high} 범위여야 합니다")
    if start == end:
        raise RuleSchemaError(f"{label}: 시작과 끝이 같을 수 없습니다")
    return [start, end]


def _validate_condition(raw: Any, index: int) -> Condition:
    label = f"조건 {index + 1}"
    if not isinstance(raw, dict):
        raise RuleSchemaError(f"{label}: 형식이 올바르지 않습니다")
    field_name = raw.get("field")
    op = raw.get("op")
    value = raw.get("value")
    if field_name not in _FIELD_OPS:
        raise RuleSchemaError(f"{label}: 알 수 없는 항목입니다({field_name})")
    if op not in _FIELD_OPS[field_name]:
        raise RuleSchemaError(f"{label}: '{field_name}'에는 '{op}' 비교를 쓸 수 없습니다")
    compiled = None
    if field_name == "has_attachment":
        if not isinstance(value, bool):
            raise RuleSchemaError(f"{label}: 첨부 여부는 예/아니오여야 합니다")
    elif field_name == "auth":
        if value not in AUTH_VALUES:
            raise RuleSchemaError(f"{label}: 인증 조건 값이 올바르지 않습니다")
    elif field_name == "received_hour":
        if op == "between":
            value = _check_range(value, label, low=0, high=24)
        elif not (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 23):
            raise RuleSchemaError(f"{label}: 시각은 0~23이어야 합니다")
    elif field_name == "received_weekday":
        if op == "between":
            value = _check_range(value, label, low=0, high=7)
        elif not (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6):
            raise RuleSchemaError(f"{label}: 요일은 0(월)~6(일)이어야 합니다")
    elif op == "in_list":
        if not isinstance(value, list | tuple) or not value:
            raise RuleSchemaError(f"{label}: 목록이 비어 있습니다")
        if len(value) > MAX_LIST_ITEMS:
            raise RuleSchemaError(f"{label}: 목록은 {MAX_LIST_ITEMS}개 이하여야 합니다")
        items: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > MAX_LIST_ITEM_CHARS:
                raise RuleSchemaError(f"{label}: 목록 항목이 올바르지 않습니다")
            items.append(item.strip().lower())
        value = items
    elif op == "regex":
        value = _check_text(value, label)
        try:
            compiled = compile_rule_regex(value)
        except RegexError as exc:
            raise RuleSchemaError(f"{label}: {exc}") from exc
    else:
        value = _check_text(value, label)
    return Condition(field=field_name, op=op, value=value, compiled=compiled)


def parse_conditions(raw: Any) -> ConditionSet:
    """조건 JSON(문자열 또는 dict)을 검증하고 정규식을 컴파일한다. 실패하면 RuleSchemaError."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuleSchemaError("조건 형식(JSON)이 올바르지 않습니다") from exc
    if not isinstance(raw, dict):
        raise RuleSchemaError("조건 형식이 올바르지 않습니다")
    match = raw.get("match", "all")
    if match not in MATCH_MODES:
        raise RuleSchemaError("조건 결합 방식은 '모두(all)' 또는 '하나라도(any)'여야 합니다")
    items = raw.get("conditions", [])
    if not isinstance(items, list):
        raise RuleSchemaError("조건 목록 형식이 올바르지 않습니다")
    if len(items) > MAX_CONDITIONS:
        raise RuleSchemaError(f"조건은 {MAX_CONDITIONS}개 이하여야 합니다")
    return ConditionSet(
        match=match, conditions=tuple(_validate_condition(c, i) for i, c in enumerate(items))
    )


def conditions_to_json(conditions: ConditionSet) -> str:
    """검증된 조건을 저장용 정규 JSON으로(컴파일 객체 제외)."""
    return json.dumps(
        {
            "match": conditions.match,
            "conditions": [
                {"field": c.field, "op": c.op, "value": c.value} for c in conditions.conditions
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def template_slots(template: str) -> list[str]:
    return _SLOT_RE.findall(template)


def _has_sender_whitelist(conditions: ConditionSet) -> bool:
    return conditions.match == "all" and any(
        c.op == "in_list" and c.field in ("from_address", "from_domain")
        for c in conditions.conditions
    )


def validate_rule(data: dict[str, Any], *, allow_auto_send: bool) -> ValidatedRule:
    """편집 창이 넘긴 값을 검증해 repo 저장용 필드로 바꾼다(G0, §7.9).

    Args:
        allow_auto_send: Phase B에서는 False — auto_send 저장을 거부한다.
    """
    warnings: list[str] = []
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuleSchemaError("규칙 이름을 입력하세요")
    if len(name) > MAX_NAME_CHARS:
        raise RuleSchemaError(f"규칙 이름은 {MAX_NAME_CHARS}자 이하여야 합니다")

    account_id = data.get("account_id")
    if account_id is not None and not (
        isinstance(account_id, int) and not isinstance(account_id, bool) and account_id >= 1
    ):
        raise RuleSchemaError("계정 선택이 올바르지 않습니다")

    action = data.get("action")
    if action not in ACTIONS_ALL:
        raise RuleSchemaError("동작을 선택하세요")
    if action == "auto_send" and not allow_auto_send:
        raise RuleSchemaError(AUTOSEND_NOT_YET_MESSAGE)

    reply_mode = data.get("reply_mode", "generated")
    if reply_mode not in REPLY_MODES:
        raise RuleSchemaError("응답 방식이 올바르지 않습니다")

    conditions = parse_conditions(data.get("conditions", {"match": "all", "conditions": []}))
    if not conditions.conditions:
        warnings.append("조건이 없어 이 규칙은 받은 모든 메일에 맞습니다")
    if any(c.field == "auth" for c in conditions.conditions):
        warnings.append("인증 조건은 현재 버전에서 평가되지 않아 항상 '맞지 않음'으로 처리됩니다")

    fixed_template = data.get("fixed_template") or None
    if reply_mode == "fixed_template":
        if not isinstance(fixed_template, str) or not fixed_template.strip():
            raise RuleSchemaError("고정 템플릿 내용을 입력하세요")
        if len(fixed_template) > MAX_TEMPLATE_CHARS:
            raise RuleSchemaError(f"고정 템플릿은 {MAX_TEMPLATE_CHARS}자 이하여야 합니다")
        unknown = [s for s in template_slots(fixed_template) if s not in TEMPLATE_SLOTS]
        if unknown:
            raise RuleSchemaError(
                "고정 템플릿에 쓸 수 없는 자리표시자가 있습니다: "
                + ", ".join("{" + s + "}" for s in unknown)
            )
    elif fixed_template is not None and not isinstance(fixed_template, str):
        raise RuleSchemaError("고정 템플릿 형식이 올바르지 않습니다")

    extra = data.get("extra_instructions") or None
    if extra is not None and (not isinstance(extra, str) or len(extra) > MAX_INSTRUCTIONS_CHARS):
        raise RuleSchemaError(f"추가 지침은 {MAX_INSTRUCTIONS_CHARS}자 이하여야 합니다")

    max_chars = data.get("max_chars", 400)
    if not (
        isinstance(max_chars, int) and not isinstance(max_chars, bool) and 50 <= max_chars <= 2000
    ):
        raise RuleSchemaError("최대 글자 수는 50~2000이어야 합니다")

    cooldown = data.get("cooldown_hours")
    if cooldown is not None and not (
        isinstance(cooldown, int) and not isinstance(cooldown, bool) and 1 <= cooldown <= 720
    ):
        raise RuleSchemaError("쿨다운은 1~720시간이어야 합니다")

    allow_thread = bool(data.get("allow_thread_context", False))
    if action == "auto_send":
        # A5: 발신자 화이트리스트(match=all 안의 from_address/from_domain in_list) 없으면 차단.
        if not _has_sender_whitelist(conditions):
            raise RuleSchemaError(
                "즉시발송 규칙에는 '모두 만족' 조건 안에 "
                "발신자 주소/도메인 목록(in_list)이 필요합니다"
            )
        for cond in conditions.conditions:
            if cond.op == "in_list" and cond.field == "from_domain":
                free = sorted(set(cond.value) & FREE_MAIL_DOMAINS)
                if free:
                    warnings.append("무료 메일 도메인이 발신자 목록에 있습니다: " + ", ".join(free))
        if allow_thread:
            warnings.append("스레드 컨텍스트 허용은 이전 메일 내용이 Claude에 전달됩니다")

    enabled = bool(data.get("enabled", True))
    fields = {
        "name": name.strip(),
        "account_id": account_id,
        "conditions_json": conditions_to_json(conditions),
        "action": action,
        "reply_mode": reply_mode,
        "fixed_template": fixed_template,
        "extra_instructions": extra,
        "max_chars": int(max_chars),
        "allow_thread_context": allow_thread,
        "accept_aligned_dkim": bool(data.get("accept_aligned_dkim", False)),
        "cooldown_hours": cooldown,
    }
    return ValidatedRule(fields=fields, enabled=enabled, warnings=warnings)
