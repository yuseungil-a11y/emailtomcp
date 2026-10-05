from __future__ import annotations

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
