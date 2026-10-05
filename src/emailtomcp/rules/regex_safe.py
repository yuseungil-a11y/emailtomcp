"""규칙 정규식 — google-re2(선형 시간) 전용 (DESIGN.md §7.1 M3).

- 패턴은 200자 이하. 저장할 때 컴파일을 검증한다(`compile_rule_regex`).
- RE2는 역참조(`\\1`)와 전후방탐색(`(?=`, `(?<=` 등)을 지원하지 않는다 — 컴파일 오류로 거부된다.
- 대소문자는 구분하지 않는다(패턴 앞에 `(?i)`를 붙인다). 사용자가 직접 `(?i)`를 넣어도 된다.
- 본문은 앞 10,000자만 검사한다(`BODY_SCAN_CHARS`, 호출자가 자른다).

표준 `re` 모듈로 폴백하지 않는다 — 백트래킹 폭주(ReDoS)를 막는 것이 이 모듈의 목적이다.
"""

from __future__ import annotations

from typing import Any

import re2

MAX_PATTERN_CHARS = 200
BODY_SCAN_CHARS = 10_000

# 인스턴스마다 RE2 메모리 상한(바이트). 기본 8MB보다 작게 둬 거대한 DFA를 막는다.
_MAX_MEM = 2 << 20


class RegexError(ValueError):
    """규칙 정규식이 쓸 수 없는 형태다(길이 초과·문법 오류·RE2 미지원 구문)."""


def compile_rule_regex(pattern: str) -> Any:
    """규칙 정규식을 RE2로 컴파일한다. 실패하면 사용자에게 보여 줄 한국어 메시지로 RegexError."""
    if not isinstance(pattern, str) or not pattern:
        raise RegexError("정규식이 비어 있습니다")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise RegexError(f"정규식은 {MAX_PATTERN_CHARS}자 이하여야 합니다")
    options = re2.Options()
    options.case_sensitive = False
    options.max_mem = _MAX_MEM
    options.log_errors = False
    try:
        return re2.compile(pattern, options)
    except Exception as exc:  # noqa: BLE001 — re2.error 등 구현별 예외를 하나로 감싼다
        raise RegexError(
            "정규식 문법 오류이거나 RE2가 지원하지 않는 구문입니다"
            "(역참조·전후방탐색은 쓸 수 없습니다)"
        ) from exc


def regex_search(compiled: Any, text: str) -> bool:
    return compiled.search(text) is not None
