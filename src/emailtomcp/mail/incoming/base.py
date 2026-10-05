"""수신 프로토콜 추상화 — `IncomingProvider` Protocol (DESIGN.md §2(b)).

`connect/list_new/fetch/set_flags/move/delete/list_folders`를 제공한다. `move`는
IMAP에서 P1 규칙(MOVE 우선, 없으면 COPY+\\Deleted+UID EXPUNGE)을 따르고, POP3에서는
아무 동작도 하지 않는다(로컬 이동은 상위 서비스가 처리한다, §2(b) 추상화 최소화 S-18).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RemoteFolder:
    """원격 폴더 하나. `role`은 SPECIAL-USE 또는 이름 후보 매칭 결과(§2(b))."""

    name: str
    role: str | None = None
    delimiter: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteMessageSummary:
    """신규 메일 목록 조회 결과(아직 본문은 받지 않음)."""

    remote_uid: str
    size_bytes: int | None = None
    flags: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class FetchedMessage:
    """`fetch()` 결과 — 원문 바이트와 현재 플래그."""

    remote_uid: str
    raw: bytes
    flags: frozenset[str] = field(default_factory=frozenset)


@runtime_checkable
class IncomingProvider(Protocol):
    """POP3/IMAP 공용 수신 포트. 운영 구현은 `pop3.Pop3IncomingProvider`/
    `imap.ImapIncomingProvider`이고, 테스트는 `tests/fakes/fake_pop3_server.py`/
    `fake_imap_server.py`를 실제 소켓으로 띄워 통합 테스트한다.
    """

    def connect(self) -> None:
        """서버에 연결하고 인증한다. 실패 시 `core.errors`의 적절한 예외를 던진다."""
        ...

    def list_folders(self) -> list[RemoteFolder]:
        """서버 폴더 목록을 돌려준다. POP3는 INBOX 하나만 돌려준다."""
        ...

    def list_new(self, folder: str, *, known_uids: set[str]) -> list[RemoteMessageSummary]:
        """`known_uids`에 없는 신규 메일 요약을 돌려준다."""
        ...

    def fetch(self, folder: str, remote_uid: str) -> FetchedMessage:
        """메일 원문을 받아온다."""
        ...

    def set_flags(
        self,
        folder: str,
        remote_uid: str,
        *,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> None:
        """읽음/플래그를 서버에 동기화한다. POP3는 아무 동작도 하지 않는다(서버 플래그 없음)."""
        ...

    def move(self, folder: str, remote_uid: str, dest_folder: str) -> str | None:
        """메일을 다른 폴더로 옮긴다. 이동 후 새 remote_uid(모르면 None)를 돌려준다.

        POP3는 아무 동작도 하지 않고 None을 돌려준다(로컬 전용 이동은 상위 서비스가 한다).
        """
        ...

    def delete(self, folder: str, remote_uid: str) -> None:
        """영구 삭제(또는 그에 준하는 동작)를 수행한다."""
        ...

    def close(self) -> None:
        """연결을 정리한다(커밋/로그아웃 포함)."""
        ...
