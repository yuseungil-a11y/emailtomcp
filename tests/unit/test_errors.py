from __future__ import annotations

from emailtomcp.core.error_codes import ErrorCode
from emailtomcp.core.errors import (
    AuthError,
    EmailToMcpError,
    PermanentError,
    PolicyError,
    SecurityError,
    TransientError,
)


def test_error_hierarchy() -> None:
    assert issubclass(TransientError, EmailToMcpError)
    assert issubclass(PermanentError, EmailToMcpError)
    assert issubclass(AuthError, PermanentError)
    assert issubclass(AuthError, EmailToMcpError)
    # §4.4: 정책/보안 위반은 재시도 대상(Transient)도, 단순 영구 오류(Permanent)도 아니다.
    assert issubclass(PolicyError, EmailToMcpError)
    assert issubclass(SecurityError, EmailToMcpError)
    assert not issubclass(PolicyError, (TransientError, PermanentError))
    assert not issubclass(SecurityError, (TransientError, PermanentError))


def test_errors_are_raisable_and_catchable_by_base() -> None:
    try:
        raise AuthError("인증 실패")
    except EmailToMcpError as exc:
        assert str(exc) == "인증 실패"
    else:
        raise AssertionError("예외가 발생해야 한다")


def test_error_code_defaults_to_none() -> None:
    exc = AuthError("인증 실패")
    assert exc.error_code is None


def test_error_code_can_be_attached_via_kwarg() -> None:
    exc = AuthError("IMAP 인증 실패", error_code=ErrorCode.IMAP_AUTH_FAILED)
    assert exc.error_code is ErrorCode.IMAP_AUTH_FAILED
    assert str(exc) == "IMAP 인증 실패"


def test_error_code_survives_reraise_through_base_class() -> None:
    try:
        raise TransientError("SMTP 통신 실패", error_code=ErrorCode.SMTP_COMM_FAILED)
    except EmailToMcpError as exc:
        assert exc.error_code is ErrorCode.SMTP_COMM_FAILED
