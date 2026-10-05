"""로그/진단 전용 에러코드 중앙 레지스트리.

**이것은 `autoreply`/`rules` 쪼의 `reason_code`(예: `g6_tools_mismatch`,
`preflight_settings_changed`, `generation_mismatch` 등, `auto_reply_log` 테이블에
기록되는 **비즈니스 로직 분기용** 문자열 코드)와는 완전히 별개다. `reason_code`는
건드리지 않는다.

여기서 정의하는 `ErrorCode`는 오직 **사람이 로그를 보고 "이게 무슨 상황인지" 빠르게
식별**하게 하려는 진단 목적이다 — 코드 분기 로직에 쓰지 않는다.

코드 형식은 `EMCP-NNNN`이고, 천 단위 접두사로 카테고리를 나눈다.

- `EMCP-1xxx`: 메일수신(IMAP/POP3)
- `EMCP-2xxx`: 메일발송(SMTP)
- `EMCP-3xxx`: MCP 서버
- `EMCP-4xxx`: 저장소/DB
- `EMCP-5xxx`: 자동업데이트
- `EMCP-6xxx`: UI
- `EMCP-9xxx`: 알 수 없음/일반

Enum 멤버의 `.value`(코드 문자열)는 서로 중복되면 안 된다 — `StrEnum`은 값이 같은
멤버를 그냥 별칭(alias)으로 취급해 조용히 통과시키므로, 중복 검사는
`tests/unit/test_error_codes.py`의 유닛테스트로 강제한다.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """고정 진단 코드. 멤버 이름은 영문 스네이크케이스, 값은 `EMCP-NNNN` 문자열이다."""

    # ---- EMCP-1xxx: 메일수신 (IMAP/POP3) ----
    IMAP_CONNECT_FAILED = "EMCP-1001"
    IMAP_AUTH_FAILED = "EMCP-1002"
    IMAP_OPERATION_FAILED = "EMCP-1003"  # 향후 사용 예정: 연결 후 LIST/SEARCH/FETCH/STORE 등 개별 명령 실패 시 부착
    POP3_CONNECT_FAILED = "EMCP-1010"
    POP3_AUTH_FAILED = "EMCP-1011"
    POP3_OPERATION_FAILED = "EMCP-1012"  # 향후 사용 예정: 연결 후 UIDL/RETR/DELE 등 개별 명령 실패 시 부착

    # ---- EMCP-2xxx: 메일발송 (SMTP) ----
    SMTP_CONNECT_FAILED = "EMCP-2001"
    SMTP_AUTH_FAILED = "EMCP-2002"
    SMTP_RECIPIENTS_REFUSED = "EMCP-2003"
    SMTP_RESPONSE_ERROR = "EMCP-2004"
    SMTP_COMM_FAILED = "EMCP-2005"

    # ---- EMCP-3xxx: MCP 서버 ----
    MCP_SERVER_START_FAILED = "EMCP-3001"
    MCP_PORT_UNAVAILABLE = "EMCP-3002"

    # ---- EMCP-4xxx: 저장소/DB ----
    DB_MIGRATION_FAILED = "EMCP-4001"
    DB_SCHEMA_TOO_NEW = "EMCP-4002"

    # ---- EMCP-5xxx: 자동업데이트 ----
    UPDATE_SIGNATURE_INVALID = "EMCP-5001"
    UPDATE_MANIFEST_REJECTED = "EMCP-5002"

    # ---- EMCP-6xxx: UI ----
    SINGLE_INSTANCE_LISTEN_FAILED = "EMCP-6001"

    # ---- EMCP-9xxx: 알 수 없음/일반 ----
    UNKNOWN = "EMCP-9000"  # 향후 사용 예정: 위 분류에 속하지 않는 예상치 못한 오류의 기본값


_DESCRIPTIONS: dict[ErrorCode, str] = {
    ErrorCode.IMAP_CONNECT_FAILED: (
        "IMAP 서버에 연결하지 못했습니다(네트워크/TLS/타임아웃 등 일시적 오류)."
    ),
    ErrorCode.IMAP_AUTH_FAILED: "IMAP 로그인(인증)이 거부되었습니다 — 계정/비밀번호를 확인하세요.",
    ErrorCode.IMAP_OPERATION_FAILED: (
        "연결은 됐지만 IMAP 명령(LIST/SEARCH/FETCH/STORE 등) 수행 중 오류가 났습니다."
    ),
    ErrorCode.POP3_CONNECT_FAILED: (
        "POP3 서버에 연결하지 못했습니다(네트워크/TLS/타임아웃 등 일시적 오류)."
    ),
    ErrorCode.POP3_AUTH_FAILED: "POP3 로그인(인증)이 거부되었습니다 — 계정/비밀번호를 확인하세요.",
    ErrorCode.POP3_OPERATION_FAILED: (
        "연결은 됐지만 POP3 명령(UIDL/RETR/DELE 등) 수행 중 오류가 났습니다."
    ),
    ErrorCode.SMTP_CONNECT_FAILED: (
        "SMTP 서버에 연결하지 못했거나 보안 설정(SSL/STARTTLS)이 맞지 않습니다."
    ),
    ErrorCode.SMTP_AUTH_FAILED: "SMTP 로그인(인증)이 거부되었습니다 — 계정/비밀번호를 확인하세요.",
    ErrorCode.SMTP_RECIPIENTS_REFUSED: "SMTP 서버가 수신자 전부 또는 일부를 거부했습니다.",
    ErrorCode.SMTP_RESPONSE_ERROR: "SMTP 서버가 오류 응답(4xx/5xx)을 돌려줬습니다.",
    ErrorCode.SMTP_COMM_FAILED: "메일 발송 중 SMTP 통신이 끊기거나 실패했습니다(일시적 오류).",
    ErrorCode.MCP_SERVER_START_FAILED: (
        "MCP HTTP 서버를 시작하지 못했습니다(소켓/ASGI 초기화 오류 등)."
    ),
    ErrorCode.MCP_PORT_UNAVAILABLE: (
        "MCP 서버용 포트를 다른 프로그램이 이미 사용 중이라 바인딩할 수 없습니다"
        "(자동으로 다른 포트로 바꾸지 않습니다)."
    ),
    ErrorCode.DB_MIGRATION_FAILED: (
        "DB 마이그레이션(스키마 업그레이드) 적용 중 오류가 발생해 롤백했습니다."
    ),
    ErrorCode.DB_SCHEMA_TOO_NEW: (
        "DB의 스키마 버전이 이 앱이 아는 최신 버전보다 높습니다(더 최신 버전의 앱으로 만든 DB)."
    ),
    ErrorCode.UPDATE_SIGNATURE_INVALID: (
        "자동업데이트 매니페스트의 서명(Ed25519) 검증에 실패했거나, "
        "같은 버전의 해시가 이전과 달라졌습니다 — 보안 경보."
    ),
    ErrorCode.UPDATE_MANIFEST_REJECTED: (
        "자동업데이트 매니페스트를 검증 단계에서 거부했습니다"
        "(형식 오류, 만료, 롤백 시도 등 — 보안 경보는 아님)."
    ),
    ErrorCode.SINGLE_INSTANCE_LISTEN_FAILED: (
        "단일 인스턴스 로컬 서버(QLocalServer) 바인딩에 실패해, 두 번째 실행 활성화 신호와 "
        "MCP stdio 프록시 핸드셰이크 중계를 받지 못한 채로 계속 기동했습니다."
    ),
    ErrorCode.UNKNOWN: "분류되지 않은 오류입니다.",
}


def describe(code: ErrorCode | str) -> str:
    """에러코드(Enum 멤버 또는 `"EMCP-1001"` 같은 문자열)에 대한 한국어 설명을 돌려준다.

    모르는 코드 문자열이 들어오면 예외를 내지 않고 설명 대신 안내 문구를 돌려준다
    (로그 출력 보조 용도로 쓰이므로, 이 함수 자체가 로깅을 막아서는 안 된다).
    """
    if isinstance(code, str) and not isinstance(code, ErrorCode):
        try:
            code = ErrorCode(code)
        except ValueError:
            return f"(알 수 없는 에러코드: {code})"
    return _DESCRIPTIONS.get(code, f"(설명이 등록되지 않은 에러코드: {code})")
