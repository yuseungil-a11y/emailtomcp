"""SMTP 발신 (DESIGN.md §8.7 M7).

SSL(465)/STARTTLS(587)을 지원한다. STARTTLS를 선택했는데 서버가 광고하지 않거나
협상에 실패하면 **연결을 중단한다** — 평문으로 다운그레이드하는 폴백 코드는 두지 않는다.
"""

from __future__ import annotations

import contextlib
import logging
import smtplib
import ssl

from emailtomcp.core.errors import AuthError, PermanentError, TransientError

logger = logging.getLogger(__name__)

try:
    import truststore

    _HAS_TRUSTSTORE = True
except ImportError:  # pragma: no cover - truststore 미설치 환경 폴백
    _HAS_TRUSTSTORE = False


def _ssl_context() -> ssl.SSLContext:
    if _HAS_TRUSTSTORE:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context()


def _connect(host: str, port: int, security: str, *, timeout: float) -> smtplib.SMTP:
    """SSL/STARTTLS/none에 맞게 연결하고 EHLO까지 끝낸 SMTP 커넥션을 돌려준다(§8.7 M7).

    `send_mail`과 `test_connection`(계정 설정 [연결 테스트])이 함께 쓴다.
    """
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=_ssl_context())
    if security == "starttls":
        conn = smtplib.SMTP(host, port, timeout=timeout)
        conn.ehlo()
        if not conn.has_extn("starttls"):
            raise PermanentError(
                f"SMTP 서버({host}:{port})가 STARTTLS를 지원하지 않습니다. "
                "평문으로 다운그레이드하지 않습니다(§8.7)."
            )
        conn.starttls(context=_ssl_context())
        conn.ehlo()
        return conn
    if security == "none":
        return smtplib.SMTP(host, port, timeout=timeout)
    raise PermanentError(f"알 수 없는 보안 모드입니다: {security}")


def test_connection(
    *,
    host: str,
    port: int,
    security: str,
    username: str | None,
    password: str | None,
    timeout: float = 15.0,
) -> None:
    """계정 설정 화면의 [연결 테스트] 버튼용 — 로그인까지만 확인하고 메일은 보내지 않는다."""
    conn: smtplib.SMTP | None = None
    try:
        conn = _connect(host, port, security, timeout=timeout)
        if username is not None and password is not None:
            conn.login(username, password)
    except smtplib.SMTPAuthenticationError as exc:
        raise AuthError(f"SMTP 인증 실패: {exc}") from exc
    except smtplib.SMTPResponseException as exc:
        if 500 <= exc.smtp_code < 600:
            raise PermanentError(f"SMTP 영구 오류({exc.smtp_code}): {exc.smtp_error!r}") from exc
        raise TransientError(f"SMTP 일시 오류({exc.smtp_code}): {exc.smtp_error!r}") from exc
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise TransientError(f"SMTP 통신 실패: {exc}") from exc
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.quit()


def send_mail(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    security: str,
    mail_from: str,
    rcpt_tos: list[str],
    raw_bytes: bytes,
    timeout: float = 30.0,
) -> None:
    """메일 한 통을 SMTP로 발송한다.

    성공하면 조용히 반환한다. 실패하면 `core.errors` 분류(Transient/Permanent/Auth)에
    맞는 예외를 던진다 — `send_service`의 재시도 판단은 이 분류만 본다(§4.4).
    """
    conn: smtplib.SMTP | None = None
    try:
        if security == "ssl":
            conn = smtplib.SMTP_SSL(host, port, timeout=timeout, context=_ssl_context())
        elif security == "starttls":
            conn = smtplib.SMTP(host, port, timeout=timeout)
            conn.ehlo()
            if not conn.has_extn("starttls"):
                raise PermanentError(
                    f"SMTP 서버({host}:{port})가 STARTTLS를 지원하지 않습니다. "
                    "평문으로 다운그레이드하지 않습니다(§8.7)."
                )
            conn.starttls(context=_ssl_context())
            conn.ehlo()
        elif security == "none":
            conn = smtplib.SMTP(host, port, timeout=timeout)
        else:
            raise PermanentError(f"알 수 없는 보안 모드입니다: {security}")

        if username is not None and password is not None:
            conn.login(username, password)

        conn.sendmail(mail_from, rcpt_tos, raw_bytes)
    except smtplib.SMTPAuthenticationError as exc:
        raise AuthError(f"SMTP 인증 실패: {exc}") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise PermanentError(f"SMTP 수신자 거부: {exc.recipients}") from exc
    except smtplib.SMTPResponseException as exc:
        if 500 <= exc.smtp_code < 600:
            raise PermanentError(f"SMTP 영구 오류({exc.smtp_code}): {exc.smtp_error!r}") from exc
        raise TransientError(f"SMTP 일시 오류({exc.smtp_code}): {exc.smtp_error!r}") from exc
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise TransientError(f"SMTP 통신 실패: {exc}") from exc
    finally:
        if conn is not None:
            try:
                conn.quit()
            except (smtplib.SMTPException, OSError):
                # quit() 실패는 발송 결과에 영향이 없다 — 소켓만 정리한다.
                with contextlib.suppress(OSError):
                    conn.close()
