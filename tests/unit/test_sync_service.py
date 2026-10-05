"""`SyncService` 테스트 (DESIGN.md §4.5 S-15 계정 장애 격리, §2(b) 수신→저장→이벤트)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers import make_account, make_db

from emailtomcp.core.clock import FakeClock
from emailtomcp.core.errors import AuthError
from emailtomcp.core.events import EventBus
from emailtomcp.mail.incoming.base import FetchedMessage, RemoteFolder, RemoteMessageSummary
from emailtomcp.mail.sync_service import (
    EVENT_ACCOUNT_CONNECTED,
    EVENT_ACCOUNT_ERROR,
    EVENT_MESSAGE_RECEIVED,
    SyncService,
)
from emailtomcp.storage.repositories import folders as folders_repo
from emailtomcp.storage.repositories import messages as messages_repo


class _FakeProvider:
    """IncomingProvider Protocol을 만족하는 최소 스텁."""

    def __init__(self, messages: dict[str, bytes], *, fail_connect: bool = False) -> None:
        self._messages = messages
        self._fail_connect = fail_connect
        self.closed = False

    def connect(self) -> None:
        if self._fail_connect:
            raise AuthError("가짜 인증 실패")

    def list_folders(self) -> list[RemoteFolder]:
        return [RemoteFolder(name="INBOX", role="inbox")]

    def list_new(self, folder: str, *, known_uids: set[str]) -> list[RemoteMessageSummary]:
        return [
            RemoteMessageSummary(remote_uid=uid) for uid in self._messages if uid not in known_uids
        ]

    def fetch(self, folder: str, remote_uid: str) -> FetchedMessage:
        return FetchedMessage(remote_uid=remote_uid, raw=self._messages[remote_uid])

    def set_flags(self, folder, remote_uid, *, seen=None, flagged=None) -> None:
        pass

    def move(self, folder, remote_uid, dest_folder) -> str | None:
        return None

    def delete(self, folder, remote_uid) -> None:
        pass

    def close(self) -> None:
        self.closed = True


SIMPLE_EML = (
    b"From: sender@example.com\r\nTo: me@example.com\r\nSubject: hi\r\n"
    b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\nMessage-ID: <sync-001@example.com>\r\n"
    b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
)


def _setup(tmp_path: Path):
    db = make_db(tmp_path)
    mail_dir = tmp_path / "mail"
    mail_dir.mkdir()
    bus = EventBus()
    clock = FakeClock()
    return db, mail_dir, bus, clock


def test_fetch_now_stores_message_and_publishes_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        provider = _FakeProvider({"uid-1": SIMPLE_EML})

        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)

        received = []
        bus.subscribe(EVENT_MESSAGE_RECEIVED, lambda name, payload: received.append(payload))

        saved = service.fetch_now(account_id)
        assert saved == 1
        assert len(received) == 1
        assert provider.closed is True

        conn = db.new_read_connection()
        try:
            rows = conn.execute("SELECT subject FROM messages").fetchall()
        finally:
            conn.close()
        assert [r["subject"] for r in rows] == ["hi"]
    finally:
        db.stop()


def test_poll_all_isolates_account_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        bad_account_id = make_account(db, email_address="bad@example.com")
        good_account_id = make_account(db, email_address="good@example.com")

        providers = {
            bad_account_id: _FakeProvider({}, fail_connect=True),
            good_account_id: _FakeProvider({"uid-1": SIMPLE_EML}),
        }

        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: providers[account.id])

        results = service.poll_all()

        assert isinstance(results[bad_account_id], str)  # 실패가 예외로 전체를 막지 않음
        assert results[good_account_id] == 1  # 다른 계정은 정상 처리됨
    finally:
        db.stop()


def test_parse_limited_message_is_still_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MIME 상한을 넘어도 크래시 없이 저장된다(파싱 제한 모드, §8.2)."""
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        oversized = (
            b"From: sender@example.com\r\nSubject: huge\r\n"
            b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\nMessage-ID: <huge@example.com>\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + b"A" * (51 * 1024 * 1024)
        )
        provider = _FakeProvider({"uid-huge": oversized})
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)

        received = []
        bus.subscribe(EVENT_MESSAGE_RECEIVED, lambda name, payload: received.append(payload))

        saved = service.fetch_now(account_id)
        assert saved == 1
        assert received[0]["parse_limited"] is True

        conn = db.new_read_connection()
        try:
            rows = messages_repo.list_recent(conn, folder_id=received[0]["folder_id"])
        finally:
            conn.close()
        assert len(rows) == 1
    finally:
        db.stop()


class _DummyCred:
    def get_password(self, account_id: int) -> str:
        return "dummy"


def test_fetch_now_publishes_account_connected_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """메일 서버 연결상태 아이콘(§11.1)의 근거 이벤트 — 성공하면 AccountConnected."""
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        provider = _FakeProvider({"uid-1": SIMPLE_EML})
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)

        connected: list[dict] = []
        bus.subscribe(EVENT_ACCOUNT_CONNECTED, lambda name, payload: connected.append(payload))

        service.fetch_now(account_id)

        assert len(connected) == 1
        assert connected[0]["account_id"] == account_id
    finally:
        db.stop()


def test_fetch_now_publishes_account_error_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        provider = _FakeProvider({}, fail_connect=True)
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)

        errors: list[dict] = []
        bus.subscribe(EVENT_ACCOUNT_ERROR, lambda name, payload: errors.append(payload))

        with pytest.raises(AuthError):
            service.fetch_now(account_id)

        assert len(errors) == 1
        assert errors[0]["account_id"] == account_id
    finally:
        db.stop()


def test_poll_all_publishes_account_connected_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        bad_account_id = make_account(db, email_address="bad2@example.com")
        good_account_id = make_account(db, email_address="good2@example.com")
        providers = {
            bad_account_id: _FakeProvider({}, fail_connect=True),
            good_account_id: _FakeProvider({"uid-1": SIMPLE_EML}),
        }
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: providers[account.id])

        connected: list[dict] = []
        errors: list[dict] = []
        bus.subscribe(EVENT_ACCOUNT_CONNECTED, lambda name, payload: connected.append(payload))
        bus.subscribe(EVENT_ACCOUNT_ERROR, lambda name, payload: errors.append(payload))

        service.poll_all()

        assert {c["account_id"] for c in connected} == {good_account_id}
        assert {e["account_id"] for e in errors} == {bad_account_id}
    finally:
        db.stop()


class _MutableProvider(_FakeProvider):
    """서버 받은편지함 내용을 테스트 중에 바꿀 수 있는 스텁."""

    def remove(self, uid: str) -> None:
        self._messages.pop(uid)

    def add(self, uid: str, raw: bytes) -> None:
        self._messages[uid] = raw


def _eml(n: int) -> bytes:
    return SIMPLE_EML.replace(b"sync-001", f"sync-{n:03d}".encode())


def test_backfill_is_per_folder_initial_sync_not_local_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """데카르트 31번 M-1 재현: 최초 동기화 메일만 backfill. 받은편지함을 비운(보관함으로
    이동해 로컬 INBOX 0건) 뒤 받는 새 정상 메일은 backfill이 아니다."""
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        provider = _MutableProvider({"uid-1": _eml(1), "uid-2": _eml(2)})
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)
        received: list[dict] = []
        bus.subscribe(EVENT_MESSAGE_RECEIVED, lambda name, payload: received.append(payload))

        assert service.fetch_now(account_id) == 2
        assert [p["is_backfill"] for p in received] == [True, True]  # 최초 동기화

        # 받은편지함 비우기: 서버·로컬 INBOX 모두에서 사라짐(보관함으로 이동한 것과 같은 효과)
        for uid in ("uid-1", "uid-2"):
            provider.remove(uid)
        archive = folders_repo.get_or_create_folder(db, account_id, "Archive", role="archive")
        _move_all_inbox_to(db, archive.id)
        conn = db.new_read_connection()
        try:
            inbox_count = conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN folders f ON f.id = m.folder_id "
                "WHERE f.name = 'INBOX'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert inbox_count == 0  # 로컬 INBOX 0건(예전 판정이면 다음 메일이 backfill)

        received.clear()
        provider.add("uid-3", _eml(3))
        assert service.fetch_now(account_id) == 1
        assert [p["is_backfill"] for p in received] == [False]
    finally:
        db.stop()


def _move_all_inbox_to(db, folder_id: int) -> None:  # noqa: ANN001
    def _write(conn) -> None:  # noqa: ANN001
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE messages SET folder_id = ?, remote_uid = NULL WHERE folder_id = "
            "(SELECT id FROM folders WHERE name = 'INBOX')",
            (folder_id,),
        )
        conn.commit()

    db.writer.submit(_write).result()


def test_empty_mailbox_first_sync_then_new_mail_is_not_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """빈 메일함으로 최초 동기화를 마치면, 그 뒤 처음 오는 메일은 backfill이 아니다."""
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        provider = _MutableProvider({})
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)
        received: list[dict] = []
        bus.subscribe(EVENT_MESSAGE_RECEIVED, lambda name, payload: received.append(payload))
        assert service.fetch_now(account_id) == 0
        provider.add("uid-9", _eml(9))
        assert service.fetch_now(account_id) == 1
        assert [p["is_backfill"] for p in received] == [False]
    finally:
        db.stop()


def test_partial_initial_sync_failure_keeps_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """최초 동기화 중 일부 메일 저장이 실패해도 완료 표식은 찍되, 같은 세션에서 다시 받는 그
    과거 메일은 backfill로 본다(과거 메일이 새 메일로 둔갑하지 않게, 데카르트 33번 M-5)."""
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        provider = _MutableProvider({"uid-1": _eml(1), "uid-2": _eml(2)})
        original_fetch = provider.fetch
        fail_once = {"uid-2"}

        def flaky_fetch(folder: str, uid: str) -> FetchedMessage:
            if uid in fail_once:
                fail_once.discard(uid)
                raise RuntimeError("일시 오류")
            return original_fetch(folder, uid)

        provider.fetch = flaky_fetch  # type: ignore[method-assign]
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)
        received: list[dict] = []
        bus.subscribe(EVENT_MESSAGE_RECEIVED, lambda name, payload: received.append(payload))
        assert service.fetch_now(account_id) == 1
        assert _initial_sync_done(db) is True  # 일부 실패해도 표식은 바로 찍힘
        assert service.fetch_now(account_id) == 1  # 실패했던 uid-2 재수신
        assert [p["is_backfill"] for p in received] == [True, True]
        provider.add("uid-3", _eml(3))
        received.clear()
        service.fetch_now(account_id)
        assert [p["is_backfill"] for p in received] == [False]
    finally:
        db.stop()


def _initial_sync_done(db) -> bool:  # noqa: ANN001
    conn = db.new_read_connection()
    try:
        row = conn.execute("SELECT initial_sync_done FROM folders WHERE name = 'INBOX'").fetchone()
    finally:
        conn.close()
    return bool(row and row[0])


def test_persistently_failing_mail_does_not_block_initial_sync_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """데카르트 33번 M-5 재현: uid "bad"의 fetch가 항상 실패해도 첫 동기화 직후 완료 표식이
    찍히고, 두 번째 동기화부터 새 정상 메일은 backfill이 아니다."""
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        provider = _MutableProvider({"uid-1": _eml(1), "bad": _eml(99)})
        original_fetch = provider.fetch

        def always_fail_bad(folder: str, uid: str) -> FetchedMessage:
            if uid == "bad":
                raise RuntimeError("항상 실패")
            return original_fetch(folder, uid)

        provider.fetch = always_fail_bad  # type: ignore[method-assign]
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)
        received: list[dict] = []
        bus.subscribe(EVENT_MESSAGE_RECEIVED, lambda name, payload: received.append(payload))

        assert service.fetch_now(account_id) == 1
        assert [p["is_backfill"] for p in received] == [True]
        assert _initial_sync_done(db) is True

        for n in (2, 3):
            received.clear()
            provider.add(f"uid-new-{n}", _eml(100 + n))
            assert service.fetch_now(account_id) == 1  # "bad"는 계속 실패
            assert [p["is_backfill"] for p in received] == [False]
    finally:
        db.stop()


def _set_ar_status(db, message_id: int, status: str) -> None:  # noqa: ANN001
    def _write(conn) -> None:  # noqa: ANN001
        conn.execute("BEGIN")
        conn.execute("UPDATE messages SET auto_reply_status = ? WHERE id = ?", (status, message_id))
        conn.commit()

    db.writer.submit(_write).result()


@pytest.mark.parametrize(
    ("first_status", "expected"),
    [
        ("drafted", True),  # 이미 평가 → 재수신 사본은 평가 제외
        ("ignored", True),
        (None, False),  # 아직 평가 전(NULL) 사본만 있으면 막지 않는다
    ],
)
def test_requeued_copy_of_evaluated_mail_is_not_reevaluated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_status: str | None, expected: bool
) -> None:
    """데카르트 33번 L-9: 서버 이동이 실패한 채 로컬만 휴지통으로 옮긴 메일이 다음 동기화 때
    INBOX에서 다시 내려오면(새 messages 행) Message-ID 기준으로 이미 평가된 메일이면 backfill로
    표시해 잡이 다시 생기지 않게 한다."""
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        provider = _MutableProvider({})
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)
        received: list[dict] = []
        bus.subscribe(EVENT_MESSAGE_RECEIVED, lambda name, payload: received.append(payload))
        assert service.fetch_now(account_id) == 0  # 빈 메일함으로 최초 동기화 완료

        provider.add("uid-5", _eml(5))
        assert service.fetch_now(account_id) == 1
        assert [p["is_backfill"] for p in received] == [False]
        first_id = received[0]["message_id"]
        if first_status is not None:
            _set_ar_status(db, first_id, first_status)

        # 오프라인 등으로 서버 이동은 실패, 로컬만 휴지통으로(remote_uid 유지) — 서버 INBOX엔 그대로
        trash = folders_repo.get_or_create_folder(db, account_id, "Trash", role="trash")
        messages_repo.move_to_folder(
            db, first_id, new_folder_id=trash.id, new_remote_uid="uid-5", keep_original_folder=True
        )
        received.clear()
        assert service.fetch_now(account_id) == 1  # INBOX에서 재수신(새 행)
        assert received[0]["message_id"] != first_id
        assert [p["is_backfill"] for p in received] == [expected]

        # 다른 Message-ID의 새 메일은 영향 없음
        received.clear()
        provider.add("uid-6", _eml(6))
        assert service.fetch_now(account_id) == 1
        assert [p["is_backfill"] for p in received] == [False]
    finally:
        db.stop()


def test_requeued_copy_of_backfill_mail_stays_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-9: 최초 동기화(backfill)로 평가에서 빠졌던 메일의 재수신 사본도 평가하지 않는다."""
    db, mail_dir, bus, clock = _setup(tmp_path)
    try:
        account_id = make_account(db)
        provider = _MutableProvider({"uid-1": _eml(1)})
        service = SyncService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=lambda _m: _DummyCred(),
            event_bus=bus,
            clock=clock,
        )
        monkeypatch.setattr(service, "_build_provider", lambda account: provider)
        received: list[dict] = []
        bus.subscribe(EVENT_MESSAGE_RECEIVED, lambda name, payload: received.append(payload))
        assert service.fetch_now(account_id) == 1
        assert [p["is_backfill"] for p in received] == [True]
        trash = folders_repo.get_or_create_folder(db, account_id, "Trash", role="trash")
        messages_repo.move_to_folder(
            db,
            received[0]["message_id"],
            new_folder_id=trash.id,
            new_remote_uid="uid-1",
            keep_original_folder=True,
        )
        received.clear()
        assert service.fetch_now(account_id) == 1
        assert [p["is_backfill"] for p in received] == [True]
    finally:
        db.stop()
