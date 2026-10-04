"""공통 예외 계층 (소크라테스 §5.1, P0 체크리스트 최우선 항목).

재시도 가능한 오류와 영구 오류를 명확히 구분해, 자동회신 잡 러너나 동기화 서비스가
"재시도할지 즉시 포기할지"를 예외 타입만으로 판단할 수 있게 한다.
"""

from __future__ import annotations


class EmailToMcpError(Exception):
    """EmailToMCP 전체 공통 베이스 예외."""


class TransientError(EmailToMcpError):
    """일시적 오류 — 재시도하면 성공할 수 있다(네트워크 끊김, 타임아웃, 서버 일시 장애 등)."""


class PermanentError(EmailToMcpError):
    """영구적 오류 — 재시도해도 같은 결과다(스키마 불일치, 잘못된 인자, 지원하지 않는 버전 등)."""


class AuthError(PermanentError):
    """인증 실패.

    발생하면 상위 레이어(예: 자동회신 잡 큐)는 큐 전체를 일시정지해야 한다(DESIGN.md §2.c).
    """
