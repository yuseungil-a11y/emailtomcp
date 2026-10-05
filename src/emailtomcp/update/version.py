"""버전 파싱·비교 (DESIGN.md §14.5, §15.3).

규칙
- 비교는 `packaging.version.Version`으로만 한다. **문자열 비교는 금지**한다
  (`"1.0.10" < "1.0.9"`가 참이 되는 함정 — §14.6 #4).
- 비교 전에 `v` 접두를 떼고 PEP 440으로 정규화한다(`2.0.0-rc.1` → `2.0.0rc1`).
- 파싱에 실패하면 `None`을 돌려준다 — 호출부가 "거부"로 처리한다(예외로 앱을 죽이지 않는다).
- 매니페스트의 `version`/`min_version_floor`에는 **정규형**(`X.Y.Z` 또는 `X.Y.ZrcN`)만
  허용한다. `+local`, `.devN`, `.postN`, alpha/beta, epoch, 앞자리 0은 금지한다.
"""

from __future__ import annotations

import re

from packaging.version import InvalidVersion, Version

# 매니페스트/릴리스 태그에 허용하는 정규형(§14.4 필드 규칙, §15.4 release.yml 태그 정규식과 동일).
_CANONICAL_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(rc(0|[1-9]\d*))?$")


def parse_version(text: object) -> Version | None:
    """문자열을 `Version`으로 바꾼다. 앞의 `v`/`V`는 뗀다. 실패하면 None."""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if candidate[:1] in ("v", "V"):
        candidate = candidate[1:]
    if not candidate:
        return None
    try:
        return Version(candidate)
    except InvalidVersion:
        return None


def normalize_version(text: object) -> str | None:
    """PEP 440 정규형 문자열을 돌려준다(`v1.0.0` → `1.0.0`, `2.0.0-rc.1` → `2.0.0rc1`)."""
    parsed = parse_version(text)
    return str(parsed) if parsed is not None else None


def is_canonical_release(text: object) -> bool:
    """매니페스트에 허용되는 정규형(`X.Y.Z` / `X.Y.ZrcN`)인지 본다.

    입력 문자열이 이미 정규형이어야 한다 — `v1.0.0`, `1.0.0-rc.1`, `1.0`, `01.0.0`은 모두 False.
    """
    if not isinstance(text, str) or not _CANONICAL_RE.fullmatch(text):
        return False
    parsed = parse_version(text)
    return parsed is not None and str(parsed) == text


def is_prerelease(text: object) -> bool:
    """rc 등 사전 릴리스인지 본다. 파싱 실패면 True(보수적으로 '안정판 아님' 취급)."""
    parsed = parse_version(text)
    return parsed is None or parsed.is_prerelease


def is_dev_build(text: object) -> bool:
    """개발 빌드인지 본다(§14.4: 버전에 `.dev`나 `+`가 있으면 개발 빌드).

    파싱이 안 되는 버전(`unknown` 등)도 개발 빌드로 취급한다 — 정식 릴리스가 아니므로
    자동 확인을 끄는 쪽이 안전하다(§15.2 "파일이 없으면 업데이트 확인을 끈다").
    """
    if not isinstance(text, str):
        return True
    parsed = parse_version(text)
    if parsed is None:
        return True
    return parsed.is_devrelease or parsed.local is not None or "+" in text or ".dev" in text


def pep440_to_semver2(text: str) -> str:
    """Velopack `--packVersion`용 SemVer2로 변환한다(§14.3, §15.3).

    `X.Y.Z` → 그대로, `X.Y.ZrcN` → `X.Y.Z-rc.N`. 정규형이 아니면 ValueError.
    """
    if not is_canonical_release(text):
        raise ValueError(f"정규형 버전이 아닙니다: {text!r}")
    parsed = Version(text)
    base = ".".join(str(p) for p in parsed.release)
    if parsed.pre is None:
        return base
    label, number = parsed.pre
    return f"{base}-{label}.{number}"
