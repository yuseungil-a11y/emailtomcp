"""공통 예외 계층 (소크라테스 §5.1, P0 체크리스트 최우선 항목).

재시도 가능한 오류와 영구 오류를 명확히 구분해, 자동회신 잡 러너나 동기화 서비스가
"재시도할지 즉시 포기할지"를 예외 타입만으로 판단할 수 있게 한다.

`error_code`(`core.error_codes.ErrorCode`)는 이 계층과는 별개 축이다 — Transient/
Permanent/Auth/Policy/Security는 "재시도 가능한가"를 판단하는 용도이고, `error_code`는
"로그에서 사람이 어떤 상황인지 바로 식별"하게 하는 순수 진단 목적이다(autoreply/rules의
`reason_code`와도 무관 — 그건 그대로 둔다). 지정하지 않으면 `None`이다.
"""

from __future__ import annotations

from emailtomcp.core.error_codes import ErrorCode


class EmailToMcpError(Exception):
    """EmailToMCP 전체 공통 베이스 예외.

    `error_code`는 키워드 인자로만 받는다(호출부의 메시지 포지셔널 인자와 섞이지
    않게). 지정하지 않은 예외는 `error_code`가 `None`이며, 로그 포매터는 그 경우
    코드를 붙이지 않는다.
    """

    def __init__(self, *args: object, error_code: ErrorCode | None = None) -> None:
        super().__init__(*args)
        self.error_code = error_code


class TransientError(EmailToMcpError):
    """일시적 오류 — 재시도하면 성공할 수 있다(네트워크 끊김, 타임아웃, 서버 일시 장애 등)."""


class PermanentError(EmailToMcpError):
    """영구적 오류 — 재시도해도 같은 결과다(스키마 불일치, 잘못된 인자, 지원하지 않는 버전 등)."""


class AuthError(PermanentError):
    """인증 실패.

    발생하면 상위 레이어(예: 자동회신 잡 큐)는 큐 전체를 일시정지해야 한다(DESIGN.md §2.c).
    """


class PolicyError(EmailToMcpError):
    """정책 위반 — 스코프, 레이트리밋, 승인 규칙, 상태 전이 경합(DESIGN.md §4.4, §5.3).

    재시도해도 정책이 바뀌지 않는 한 같은 결과다. MCP에서는 403이나 도구 오류로 응답한다.
    """


class SecurityError(EmailToMcpError):
    """보안 위반 — 허용 외 접근 시도, 서명/HMAC 검증 실패, 같은 버전인데 다른 해시 등(§4.4).

    경보를 내고 관련 기능을 정지하는 근거로 쓴다. P2 범위에서는 로컬 핸드셰이크 HMAC
    검증 실패(가짜 서버)와 허용 외 접근 시도에만 쓴다.
    """
