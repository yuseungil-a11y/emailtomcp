"""POP3 수신 provider (DESIGN.md §2(b), §8.7).

- UIDL로 중복 수신을 막는다(`pop3_uidl_seen` 테이블은 상위 서비스가 관리하고, 이 provider는
  `known_uids`를 받아 필터링만 한다).
- 서버 보관(leave_on_server)/N일후삭제 정책 판단은 상위 서비스(sync_service)가 한다.
  이 provider는 `delete()`만 제공한다.
- `move`는 로컬 전용이라 아무 동작도 하지 않는다(§2(b)).
- STARTTLS(STLS)를 선택했는데 서버가 지원하지 않거나 협상에 실패하면 예외가 그대로
  전파된다 — 평문 다운그레이드 폴백은 두지 않는다(§8.7 M7).
"""

from __future__ import annotations

import logging
import poplib
import ssl

from emailtomcp.core.errors import AuthError, PermanentError, TransientError
from emailtomcp.mail.incoming.base import FetchedMessage, RemoteFolder, RemoteMessageSummary

logger = logging.getLogger(__name__)

try:
    import truststore

    _HAS_TRUSTSTORE = True
except ImportError:  # pragma: no cover - truststore 미설치 환경 폴백
    _HAS_TRUSTSTORE = False

INBOX = "INBOX"


def _ssl_context() -> ssl.SSLContext:
    """OS 신뢰 저장소(truststore)가 있으면 그걸 쓰고, 없으면 표준 `ssl` 기본 컨텍스트로
    폴백한다(DESIGN.md §4.6, §8.7).
    """
    if _HAS_TRUSTSTORE:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context()


class Pop3IncomingProvider:
    """poplib 기반 `IncomingProvider` 구현. 폴더는 INBOX 하나뿐이다."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        security: str,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._security = security
        self._timeout = timeout
        self._conn: poplib.POP3 | None = None
        self._uidl_to_num: dict[str, int] = {}

    def connect(self) -> None:
        try:
            if self._security == "ssl":
                self._conn = poplib.POP3_SSL(
                    self._host, self._port, timeout=self._timeout, context=_ssl_context()
                )
            elif self._security == "starttls":
                conn = poplib.POP3(self._host, self._port, timeout=self._timeout)
                conn.stls(context=_ssl_context())
                self._conn = conn
            elif self._security == "none":
                self._conn = poplib.POP3(self._host, self._port, timeout=self._timeout)
            else:
                raise PermanentError(f"알 수 없는 보안 모드입니다: {self._security}")

            self._conn.user(self._username)
            self._conn.pass_(self._password)
        except poplib.error_proto as exc:
            raise AuthError(f"POP3 인증 실패: {exc}") from exc
        except (OSError, TimeoutError) as exc:
            raise TransientError(f"POP3 연결 실패: {exc}") from exc

    def _require_conn(self) -> poplib.POP3:
        if self._conn is None:
            raise PermanentError("POP3 연결이 되어 있지 않습니다(connect()를 먼저 호출)")
        return self._conn

    def list_folders(self) -> list[RemoteFolder]:
        return [RemoteFolder(name=INBOX, role="inbox")]

    def list_new(self, folder: str, *, known_uids: set[str]) -> list[RemoteMessageSummary]:
        conn = self._require_conn()
        try:
            _resp, listing, _octets = conn.uidl()
        except poplib.error_proto as exc:
            raise PermanentError(f"POP3 UIDL 실패: {exc}") from exc
        except (OSError, TimeoutError) as exc:
            raise TransientError(f"POP3 UIDL 통신 실패: {exc}") from exc

        self._uidl_to_num.clear()
        summaries: list[RemoteMessageSummary] = []
        for line in listing:
            decoded = line.decode("ascii", errors="replace")
            parts = decoded.split(" ", 1)
            if len(parts) != 2:
                continue
            msg_num_text, uidl = parts
            try:
                msg_num = int(msg_num_text)
            except ValueError:
                continue
            self._uidl_to_num[uidl] = msg_num
            if uidl not in known_uids:
                summaries.append(RemoteMessageSummary(remote_uid=uidl))
        return summaries

    def fetch(self, folder: str, remote_uid: str) -> FetchedMessage:
        conn = self._require_conn()
        msg_num = self._uidl_to_num.get(remote_uid)
        if msg_num is None:
            raise PermanentError(f"알 수 없는 UIDL입니다(list_new를 먼저 호출): {remote_uid}")
        try:
            _resp, lines, _octets = conn.retr(msg_num)
        except poplib.error_proto as exc:
            raise PermanentError(f"POP3 RETR 실패: {exc}") from exc
        except (OSError, TimeoutError) as exc:
            raise TransientError(f"POP3 RETR 통신 실패: {exc}") from exc
        raw = b"\r\n".join(lines)
        return FetchedMessage(remote_uid=remote_uid, raw=raw)

    def set_flags(
        self,
        folder: str,
        remote_uid: str,
        *,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> None:
        # POP3는 서버 쪽 플래그 개념이 없다(§2(b)). 호출은 허용하되 아무 동작도 하지 않는다.
        logger.debug("POP3 계정은 서버 플래그를 지원하지 않아 set_flags를 무시합니다")

    def move(self, folder: str, remote_uid: str, dest_folder: str) -> str | None:
        # 로컬 전용(§2(b)) — 상위 서비스가 로컬 DB에서만 폴더를 옮긴다.
        return None

    def delete(self, folder: str, remote_uid: str) -> None:
        conn = self._require_conn()
        msg_num = self._uidl_to_num.get(remote_uid)
        if msg_num is None:
            raise PermanentError(f"알 수 없는 UIDL입니다(list_new를 먼저 호출): {remote_uid}")
        try:
            conn.dele(msg_num)
        except poplib.error_proto as exc:
            raise PermanentError(f"POP3 DELE 실패: {exc}") from exc
        except (OSError, TimeoutError) as exc:
            raise TransientError(f"POP3 DELE 통신 실패: {exc}") from exc

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.quit()
        except (poplib.error_proto, OSError, TimeoutError):
            logger.warning("POP3 QUIT 실패(연결은 어차피 종료 처리함)", exc_info=True)
        finally:
            self._conn = None
            self._uidl_to_num.clear()
