"""MIME 파싱 상한 (DESIGN.md §8.2 L5, 갈릴레오 §2 MIME 상한 테스트).

메일 하나를 파싱하다 상한을 넘으면 예외를 던져 전체 플로우를 죽이지 않고,
`LimitExceeded`를 발생시켜 `mime/parse.py`가 "원문만 보관 + 파싱 제한 표시"로
안전하게 강등할 수 있게 한다.
"""

from __future__ import annotations

MAX_RAW_BYTES = 50 * 1024 * 1024  # 원문 50MB
MAX_PARTS = 500  # 파트 500개
MAX_NESTING_DEPTH = 10  # 중첩 깊이 10
MAX_HEADERS_TOTAL_BYTES = 1 * 1024 * 1024  # 헤더 총합 1MB
MAX_SINGLE_HEADER_BYTES = 64 * 1024  # 단일 헤더 64KB
MAX_DECODED_TOTAL_BYTES = 200 * 1024 * 1024  # 디코딩 후 총합 200MB
MAX_DB_BODY_BYTES = 2 * 1024 * 1024  # DB에 저장하는 본문 2MB


class LimitExceeded(Exception):
    """MIME 상한을 넘었다는 신호. 파싱을 중단시키되 앱 전체 예외 계층과는 분리한다
    (상한 초과는 오류가 아니라 "제한 모드로 전환"이라는 정상 분기이기 때문이다).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_raw_size(size: int) -> None:
    if size > MAX_RAW_BYTES:
        raise LimitExceeded(f"원문 크기 상한 초과: {size} > {MAX_RAW_BYTES}")


def check_header_block_size(size: int) -> None:
    if size > MAX_HEADERS_TOTAL_BYTES:
        raise LimitExceeded(f"헤더 총합 상한 초과: {size} > {MAX_HEADERS_TOTAL_BYTES}")


def check_single_header_size(size: int) -> None:
    if size > MAX_SINGLE_HEADER_BYTES:
        raise LimitExceeded(f"단일 헤더 상한 초과: {size} > {MAX_SINGLE_HEADER_BYTES}")


def check_part_count(count: int) -> None:
    if count > MAX_PARTS:
        raise LimitExceeded(f"파트 개수 상한 초과: {count} > {MAX_PARTS}")


def check_nesting_depth(depth: int) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise LimitExceeded(f"중첩 깊이 상한 초과: {depth} > {MAX_NESTING_DEPTH}")


def check_decoded_total(total: int) -> None:
    if total > MAX_DECODED_TOTAL_BYTES:
        raise LimitExceeded(f"디코딩 후 총합 상한 초과: {total} > {MAX_DECODED_TOTAL_BYTES}")


def clamp_db_body(text: str) -> tuple[str, bool]:
    """DB에 저장할 본문을 2MB로 제한한다. (잘라낸 텍스트, 잘렸는지 여부)를 반환한다."""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_DB_BODY_BYTES:
        return text, False
    truncated = encoded[:MAX_DB_BODY_BYTES].decode("utf-8", errors="ignore")
    return truncated, True
