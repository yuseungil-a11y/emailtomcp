"""HTML 정화 (DESIGN.md §8.4 H5, §7.5 mime/parse.py에서 재사용).

`style` 속성과 `<style> <link> <object> <iframe> <form> <meta> <base> <script>`
태그(내용까지 통째로)를 제거한다. style 속성을 없애므로 CSS로 숨긴 텍스트도 화면에
그대로 보이게 되어, "화면 표시 파트 = Claude 입력"(H5) 원칙이 성립한다.
"""

from __future__ import annotations

import nh3

# 태그와 내용을 통째로 제거한다(단순히 태그만 벗기면 숨겨진 CSS/스크립트 텍스트가
# 본문에 섞여 나올 수 있다).
_REMOVE_WITH_CONTENT = frozenset(
    {"style", "link", "object", "iframe", "form", "meta", "base", "script"}
)

# nh3 기본 ALLOWED_ATTRIBUTES에는 style 속성이 포함되지 않으므로 별도 처리가 필요 없다
# (DESIGN.md §8.4 "style 속성을 없애므로"는 이 기본 동작으로 충족된다).


def sanitize_html(html: str) -> str:
    """수신 HTML 파트를 화면 표시/Claude 입력 겸용으로 정화한다."""
    return nh3.clean(html, clean_content_tags=_REMOVE_WITH_CONTENT)
