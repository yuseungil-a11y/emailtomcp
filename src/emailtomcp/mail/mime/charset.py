"""문자셋 디코딩 (DESIGN.md §9.4).

cp949/ks_c_5601 계열 별칭을 매핑하고, 알 수 없는 문자셋은 charset_normalizer로
추정하며, 그래도 실패하면 `errors="replace"`로 최종 폴백한다. 어떤 입력으로도
예외를 던지지 않는 것이 이 모듈의 계약이다(메일 파싱이 한 통의 이상한 문자셋
때문에 중단되면 안 된다).
"""

from __future__ import annotations

import logging

from charset_normalizer import from_bytes

logger = logging.getLogger(__name__)

# 한국 메일 클라이언트/구형 서버가 흔히 쓰는 비표준 charset 라벨 -> Python codec 이름.
_CHARSET_ALIASES: dict[str, str] = {
    "ks_c_5601-1987": "cp949",
    "ks_c_5601-1989": "cp949",
    "ks_c_5601": "cp949",
    "ksc5601": "cp949",
    "ksc-5601": "cp949",
    "x-windows-949": "cp949",
    "ms949": "cp949",
    "euckr": "euc_kr",
    "euc_kr": "euc_kr",
}


def normalize_charset_name(name: str | None) -> str | None:
    """메일 헤더에 적힌 charset 라벨을 Python codec 이름으로 정규화한다."""
    if not name:
        return None
    key = name.strip().lower()
    return _CHARSET_ALIASES.get(key, key)


def decode_bytes(data: bytes, charset: str | None) -> str:
    """바이트열을 텍스트로 디코딩한다. 항상 문자열을 반환한다(예외를 던지지 않음).

    순서: (1) 명시된 charset으로 시도 → (2) charset_normalizer로 추정 → (3) utf-8
    + errors="replace" 최종 폴백.
    """
    normalized = normalize_charset_name(charset)
    if normalized:
        try:
            return data.decode(normalized, errors="strict")
        except (LookupError, UnicodeDecodeError):
            logger.debug("지정된 charset 디코딩 실패, 추정으로 폴백: %s", normalized)

    try:
        guess = from_bytes(data).best()
    except Exception:  # noqa: BLE001 — charset_normalizer 내부 오류도 최종 폴백으로 흡수
        guess = None
    if guess is not None:
        try:
            return str(guess)
        except Exception:  # noqa: BLE001
            pass

    return data.decode("utf-8", errors="replace")
