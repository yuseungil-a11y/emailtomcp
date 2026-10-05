"""디버깅용 로그 설정(app.setup_logging) 테스트.

- 보관기간 7일: `TimedRotatingFileHandler`가 매일 자정(`when="midnight"`) 회전 +
  `backupCount=7`로 설정됐는지 확인한다(실제 파일 회전은 OS 시각에 의존하므로 표준
  핸들러의 설정값을 단언하는 것으로 충분하다 — stdlib `logging.handlers`가 회전·삭제
  동작 자체를 보장한다).
- 민감정보 마스킹: `_SecretMaskingFilter`가 비밀번호/토큰/OAuth2 자격증명 패턴을
  실제로 가리는지, 마스킹 안 된 원래 값이 로그 문자열에 전혀 남지 않는지 확인한다.
"""

from __future__ import annotations

import logging
import logging.handlers

import pytest

from emailtomcp import app as app_module
from emailtomcp.config import paths
from emailtomcp.core.error_codes import ErrorCode
from emailtomcp.core.errors import AuthError


@pytest.fixture(autouse=True)
def _restore_emailtomcp_logger():
    """`setup_logging()`이 전역 "emailtomcp" 로거에 붙이는 핸들러를 테스트 뒤 정리한다."""
    logger = logging.getLogger("emailtomcp")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_excepthook = __import__("sys").excepthook
    yield
    for handler in list(logger.handlers):
        if handler not in original_handlers:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(original_level)
    __import__("sys").excepthook = original_excepthook


def test_setup_logging_rotates_daily_and_keeps_7_days(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))

    app_module.setup_logging()

    logger = logging.getLogger("emailtomcp")
    file_handlers = [
        h
        for h in logger.handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(file_handlers) == 1, "TimedRotatingFileHandler가 정확히 하나만 붙어야 한다"

    handler = file_handlers[0]
    # when="midnight"이면 stdlib가 when을 대문자로, interval을 자정 기준 하루(초)로 고정한다.
    assert handler.when == "MIDNIGHT"
    assert handler.interval == 24 * 60 * 60
    assert handler.backupCount == 7
    assert handler.baseFilename == str(tmp_path / "logs" / "emailtomcp.log")


def test_setup_logging_attaches_masking_filter_to_handlers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))

    app_module.setup_logging()

    logger = logging.getLogger("emailtomcp")
    for handler in logger.handlers:
        assert any(
            isinstance(f, app_module._SecretMaskingFilter) for f in handler.filters
        ), f"{handler}에 마스킹 필터가 없다"


def test_setup_logging_installs_excepthook(tmp_path, monkeypatch) -> None:
    import sys

    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))
    app_module.setup_logging()

    assert sys.excepthook is app_module._log_unhandled_exception


@pytest.mark.parametrize(
    ("raw_message", "secret"),
    [
        ("로그인 실패 password=SuperSecret123!", "SuperSecret123!"),
        ("MCP 토큰 token=abcdef0123456789", "abcdef0123456789"),
        ("Authorization: Bearer abc.def.ghi-signature", "abc.def.ghi-signature"),
        ("OAuth2 access_token=ya29.AbCdEf1234567890", "ya29.AbCdEf1234567890"),
        ("OAuth2 refresh_token=1//0abcdefghijklmnop", "1//0abcdefghijklmnop"),
        ("OAuth2 client_secret=GOCSPX-abcdefghijklmnop", "GOCSPX-abcdefghijklmnop"),
    ],
)
def test_secret_masking_filter_hides_sensitive_values(raw_message: str, secret: str) -> None:
    masking_filter = app_module._SecretMaskingFilter()
    record = logging.LogRecord(
        name="emailtomcp.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=raw_message,
        args=None,
        exc_info=None,
    )

    kept = masking_filter.filter(record)

    assert kept is True
    masked_message = record.getMessage()
    assert secret not in masked_message, "원본 민감값이 마스킹 뒤에도 남아있다"
    assert "***" in masked_message


def test_secret_masking_filter_leaves_normal_messages_untouched() -> None:
    masking_filter = app_module._SecretMaskingFilter()
    record = logging.LogRecord(
        name="emailtomcp.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="계정 1 수신 동기화 완료: 3건 저장",
        args=None,
        exc_info=None,
    )

    masking_filter.filter(record)

    assert record.getMessage() == "계정 1 수신 동기화 완료: 3건 저장"


class TestErrorCodeFormatter:
    """`_ErrorCodeFormatter` — 예외/에러 로그에 `EMCP-XXXX` 코드를 덧붙이는지 확인."""

    def _formatter(self) -> app_module._ErrorCodeFormatter:
        return app_module._ErrorCodeFormatter("%(levelname)s %(message)s")

    def test_appends_code_from_exc_info_attribute(self) -> None:
        formatter = self._formatter()
        try:
            raise AuthError("IMAP 인증 실패", error_code=ErrorCode.IMAP_AUTH_FAILED)
        except AuthError:
            record = logging.LogRecord(
                name="emailtomcp.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="IMAP 연결 실패",
                args=None,
                exc_info=__import__("sys").exc_info(),
            )

        formatted = formatter.format(record)

        assert "EMCP-1002" in formatted

    def test_appends_code_from_extra_even_without_exc_info(self) -> None:
        formatter = self._formatter()
        record = logging.LogRecord(
            name="emailtomcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="포트를 사용할 수 없습니다",
            args=None,
            exc_info=None,
        )
        record.error_code = ErrorCode.MCP_PORT_UNAVAILABLE

        formatted = formatter.format(record)

        assert "EMCP-3002" in formatted

    def test_plain_log_without_error_code_is_unchanged(self) -> None:
        formatter = self._formatter()
        record = logging.LogRecord(
            name="emailtomcp.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="계정 1 수신 동기화 완료: 3건 저장",
            args=None,
            exc_info=None,
        )

        formatted = formatter.format(record)

        assert "EMCP-" not in formatted
        assert formatted == "INFO 계정 1 수신 동기화 완료: 3건 저장"

    def test_does_not_mutate_record_for_reuse_across_handlers(self) -> None:
        """같은 포매터 인스턴스를 여러 핸들러가 공유해도 코드가 중복으로 붙지 않아야 한다."""
        formatter = self._formatter()
        record = logging.LogRecord(
            name="emailtomcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="메시지",
            args=None,
            exc_info=None,
        )
        record.error_code = ErrorCode.UNKNOWN

        first = formatter.format(record)
        second = formatter.format(record)

        assert first == second
        assert first.count("EMCP-9000") == 1


def test_setup_logging_uses_error_code_formatter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))

    app_module.setup_logging()

    logger = logging.getLogger("emailtomcp")
    for handler in logger.handlers:
        assert isinstance(handler.formatter, app_module._ErrorCodeFormatter)
