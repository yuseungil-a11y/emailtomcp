from __future__ import annotations

from emailtomcp.core.errors import AuthError, EmailToMcpError, PermanentError, TransientError


def test_error_hierarchy() -> None:
    assert issubclass(TransientError, EmailToMcpError)
    assert issubclass(PermanentError, EmailToMcpError)
    assert issubclass(AuthError, PermanentError)
    assert issubclass(AuthError, EmailToMcpError)


def test_errors_are_raisable_and_catchable_by_base() -> None:
    try:
        raise AuthError("인증 실패")
    except EmailToMcpError as exc:
        assert str(exc) == "인증 실패"
    else:
        raise AssertionError("예외가 발생해야 한다")
