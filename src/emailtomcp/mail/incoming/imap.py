"""IMAP 수신 provider (DESIGN.md §2(b) N-01 보정, §8.7).

- 이동은 MOVE(RFC 6851)를 우선 쓰고, 지원하지 않으면 COPY + `\\Deleted` + UID EXPUNGE로
  처리한다. UIDPLUS(RFC 4315)가 없으면 EXPUNGE를 하지 않고 `\\Deleted`만 남긴다(다른 메일이
  함께 지워지는 사고를 막기 위함). COPYUID 응답이 있으면 새 UID를 그 자리에서 돌려주고,
  없으면 호출자(상위 서비스)가 다음 동기화에서 Message-ID/크기로 매칭해야 한다.
- 휴지통·보낸편지함 폴더는 SPECIAL-USE(RFC 6154) 플래그로 먼저 찾고, 없으면 영문/한글
  이름 후보 목록으로 찾는다.
- STARTTLS가 광고되지 않거나 협상에 실패하면 예외가 그대로 전파된다 — 평문 다운그레이드
  폴백 코드는 존재하지 않는다(§8.7 M7).
"""

from __future__ import annotations

import logging
import re
import ssl

from imapclient import FLAGGED, SEEN, IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from emailtomcp.core.error_codes import ErrorCode
from emailtomcp.core.errors import AuthError, PermanentError, TransientError
from emailtomcp.mail.incoming.base import FetchedMessage, RemoteFolder, RemoteMessageSummary

logger = logging.getLogger(__name__)

try:
    import truststore

    _HAS_TRUSTSTORE = True
except ImportError:  # pragma: no cover
    _HAS_TRUSTSTORE = False

# SPECIAL-USE(RFC 6154) 플래그 -> 우리 FolderRole 문자열.
_FLAG_TO_ROLE: dict[bytes, str] = {
    b"\\Sent": "sent",
    b"\\Trash": "trash",
    b"\\Drafts": "drafts",
    b"\\Junk": "junk",
    b"\\Archive": "archive",
}

# SPECIAL-USE가 없을 때 쓰는 이름 후보(영문 + 국내 서비스 한글 표기, §2(b)).
_NAME_CANDIDATES: dict[str, tuple[str, ...]] = {
    "sent": ("Sent", "Sent Items", "Sent Messages", "Sent Mail", "보낸편지함", "보낸 편지함"),
    "trash": (
        "Trash",
        "Deleted Items",
        "Deleted Messages",
        "Deleted",
        "휴지통",
        "지운편지함",
        "지운 편지함",
    ),
    "drafts": ("Drafts", "임시보관함", "임시 보관함"),
    "junk": ("Junk", "Spam", "스팸메일함", "스팸"),
    "archive": ("Archive", "보관함"),
}
_NAME_TO_ROLE: dict[str, str] = {
    name.lower(): role for role, names in _NAME_CANDIDATES.items() for name in names
}

_COPYUID_RE = re.compile(r"COPYUID\s+\d+\s+[\d:,]+\s+([\d:,]+)", re.IGNORECASE)


def _ssl_context() -> ssl.SSLContext:
    if _HAS_TRUSTSTORE:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context()


def _parse_copyuid(response: object) -> str | None:
    """COPY/MOVE 응답 문자열에서 COPYUID(UIDPLUS)로 받은 대상 UID를 뽑아낸다."""
    if response is None:
        return None
    text = response.decode("ascii", "ignore") if isinstance(response, bytes) else str(response)
    match = _COPYUID_RE.search(text)
    if not match:
        return None
    dest_uids = match.group(1)
    first = dest_uids.split(",")[0].split(":")[0]
    return first or None


def _decode_flags(raw_flags: tuple[bytes | str, ...] | list[bytes | str] | None) -> frozenset[str]:
    return frozenset(f.decode() if isinstance(f, bytes) else f for f in (raw_flags or ()))


class ImapIncomingProvider:
    """imapclient 기반 `IncomingProvider` 구현(`use_uid=True`로 항상 UID 기준 동작)."""

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
        self._client: IMAPClient | None = None
        self._selected: tuple[str, bool] | None = None

    def connect(self) -> None:
        try:
            if self._security == "ssl":
                client = IMAPClient(
                    self._host,
                    port=self._port,
                    ssl=True,
                    ssl_context=_ssl_context(),
                    timeout=self._timeout,
                )
            elif self._security == "starttls":
                client = IMAPClient(self._host, port=self._port, ssl=False, timeout=self._timeout)
                client.starttls(_ssl_context())
            elif self._security == "none":
                client = IMAPClient(self._host, port=self._port, ssl=False, timeout=self._timeout)
            else:
                raise PermanentError(f"알 수 없는 보안 모드입니다: {self._security}")

            client.login(self._username, self._password)
            self._client = client
            self._selected = None
        except LoginError as exc:
            raise AuthError(
                f"IMAP 인증 실패: {exc}", error_code=ErrorCode.IMAP_AUTH_FAILED
            ) from exc
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(
                f"IMAP 연결 실패: {exc}", error_code=ErrorCode.IMAP_CONNECT_FAILED
            ) from exc

    def _require_client(self) -> IMAPClient:
        if self._client is None:
            raise PermanentError("IMAP 연결이 되어 있지 않습니다(connect()를 먼저 호출)")
        return self._client

    def _select(self, folder: str, *, readonly: bool) -> None:
        client = self._require_client()
        if self._selected == (folder, readonly):
            return
        try:
            client.select_folder(folder, readonly=readonly)
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP 폴더 선택 실패({folder}): {exc}") from exc
        self._selected = (folder, readonly)

    def _role_of(self, flags: tuple, name: str) -> str | None:
        for flag in flags:
            role = _FLAG_TO_ROLE.get(flag)
            if role:
                return role
        if name.upper() == "INBOX":
            return "inbox"
        return _NAME_TO_ROLE.get(name.lower())

    def list_folders(self) -> list[RemoteFolder]:
        client = self._require_client()
        try:
            raw_folders = client.list_folders()
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP LIST 실패: {exc}") from exc

        result: list[RemoteFolder] = []
        for flags, delimiter, name in raw_folders:
            role = self._role_of(flags, name)
            delim_text = delimiter.decode() if isinstance(delimiter, bytes) else delimiter
            result.append(RemoteFolder(name=name, role=role, delimiter=delim_text))
        return result

    def find_role_folder(self, role: str) -> str | None:
        """SPECIAL-USE 또는 이름 후보로 역할에 맞는 폴더명을 찾는다(§2(b))."""
        for folder in self.list_folders():
            if folder.role == role:
                return folder.name
        return None

    def list_new(self, folder: str, *, known_uids: set[str]) -> list[RemoteMessageSummary]:
        self._select(folder, readonly=True)
        client = self._require_client()
        try:
            uids = client.search(["ALL"])
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP SEARCH 실패: {exc}") from exc

        new_uids = [uid for uid in uids if str(uid) not in known_uids]
        if not new_uids:
            return []
        try:
            fetched = client.fetch(new_uids, ["FLAGS", "RFC822.SIZE"])
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP FETCH(요약) 실패: {exc}") from exc

        summaries: list[RemoteMessageSummary] = []
        for uid in new_uids:
            data = fetched.get(uid, {})
            summaries.append(
                RemoteMessageSummary(
                    remote_uid=str(uid),
                    size_bytes=data.get(b"RFC822.SIZE"),
                    flags=_decode_flags(data.get(b"FLAGS")),
                )
            )
        return summaries

    def fetch(self, folder: str, remote_uid: str) -> FetchedMessage:
        self._select(folder, readonly=True)
        client = self._require_client()
        uid = int(remote_uid)
        try:
            # BODY.PEEK[]는 RFC822와 달리 \Seen을 암묵적으로 세우지 않는다(읽음 표시는
            # set_flags로 명시적으로만 한다).
            data = client.fetch([uid], ["BODY.PEEK[]", "FLAGS"])
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP FETCH 실패: {exc}") from exc

        entry = data.get(uid)
        if not entry:
            raise PermanentError(f"IMAP FETCH 결과에 UID {uid}가 없습니다(folder={folder})")
        raw = entry.get(b"BODY[]") or entry.get(b"RFC822") or b""
        return FetchedMessage(
            remote_uid=remote_uid, raw=raw, flags=_decode_flags(entry.get(b"FLAGS"))
        )

    def set_flags(
        self,
        folder: str,
        remote_uid: str,
        *,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> None:
        self._select(folder, readonly=False)
        client = self._require_client()
        uid = int(remote_uid)
        try:
            if seen is True:
                client.add_flags([uid], [SEEN])
            elif seen is False:
                client.remove_flags([uid], [SEEN])
            if flagged is True:
                client.add_flags([uid], [FLAGGED])
            elif flagged is False:
                client.remove_flags([uid], [FLAGGED])
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP STORE(플래그) 실패: {exc}") from exc

    def move(self, folder: str, remote_uid: str, dest_folder: str) -> str | None:
        """메일을 이동하고, 가능하면 COPYUID(UIDPLUS)로 받은 새 UID를 돌려준다.

        **알려진 제약(실측 확인, 2026-10-04)**: `use_uid=True`로 쓰는 `imapclient`가
        `copy()`/`move()`를 내부적으로 `imaplib.IMAP4.uid()`를 거쳐 호출하는데, 이
        경로는 SEARCH/SORT/THREAD가 아닌 명령에 대해 태그된 완료 응답(COPYUID가 담긴
        `"... OK [COPYUID ...] ..."` 텍스트)을 버리고 엉뚱하게 untagged FETCH 응답을
        찾는다(`imaplib.IMAP4.uid` 구현의 `name = 'FETCH'` 분기). 그 결과 **UIDPLUS를
        지원하는 서버에서도 이 라이브러리 조합으로는 COPYUID를 돌려받지 못하고 항상
        `None`이 된다** — fake IMAP 서버로 실제 응답을 떠서 확인했다(테스트 참고).
        `_parse_copyuid`는 이 상황이 바뀌거나(라이브러리 업데이트, 또는 향후 저수준
        명령으로 우회) 대비해 남겨 두지만, 호출자는 **반환값이 거의 항상 None이라고
        가정하고 "다음 동기화에서 Message-ID/크기로 매칭" 경로(§2(b))를 기본으로
        구현해야 한다.**
        """
        self._select(folder, readonly=False)
        client = self._require_client()
        uid = int(remote_uid)
        try:
            if client.has_capability("MOVE"):
                response = client.move([uid], dest_folder)
                return _parse_copyuid(response)

            response = client.copy([uid], dest_folder)
            new_uid = _parse_copyuid(response)
            client.delete_messages([uid])
            if client.has_capability("UIDPLUS"):
                client.uid_expunge([uid])
            # UIDPLUS가 없으면 \Deleted만 남기고 EXPUNGE하지 않는다 — 같은 폴더의 다른
            # \Deleted 메일이 함께 지워지는 사고를 막기 위함이다(§2(b)). 실제 정리는
            # "휴지통 비우기"(purge_folder)에서 한다.
            return new_uid
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP 이동 실패({folder} -> {dest_folder}): {exc}") from exc

    def delete(self, folder: str, remote_uid: str) -> None:
        """영구 삭제 — `\\Deleted` 플래그를 붙이고, UIDPLUS가 있으면 해당 UID만 EXPUNGE한다."""
        self._select(folder, readonly=False)
        client = self._require_client()
        uid = int(remote_uid)
        try:
            client.delete_messages([uid])
            if client.has_capability("UIDPLUS"):
                client.uid_expunge([uid])
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP 삭제 실패: {exc}") from exc

    def purge_folder(self, folder: str) -> None:
        """폴더 전체를 압축(전체 EXPUNGE)한다. UIDPLUS가 없는 서버에서 "휴지통 비우기"를
        할 때만 호출해야 한다(다른 폴더에 영향 없음 — 이 폴더에 한정된 동작).
        """
        self._select(folder, readonly=False)
        client = self._require_client()
        try:
            client.expunge()
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP EXPUNGE 실패: {exc}") from exc

    def append_message(self, folder: str, raw: bytes, *, seen: bool = True) -> None:
        """보낸 메일을 Sent 폴더에 APPEND한다(§2(b), `send_service`에서 발송 성공 후 호출).

        IMAP 계정에서만 의미가 있다(POP3 계정은 보낸편지함이 로컬 전용이라 호출하지 않는다).
        """
        client = self._require_client()
        flags = [SEEN] if seen else []
        try:
            client.append(folder, raw, flags=flags)
        except (IMAPClientError, OSError, TimeoutError) as exc:
            raise TransientError(f"IMAP APPEND 실패({folder}): {exc}") from exc

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.logout()
        except (IMAPClientError, OSError, TimeoutError):
            logger.warning("IMAP LOGOUT 실패(연결은 어차피 종료 처리함)", exc_info=True)
        finally:
            self._client = None
            self._selected = None
