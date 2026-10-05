"""`SendService` 상태 전이 테스트 (DESIGN.md §5.3 Outbox, §4.4 재시도 분류)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers import make_account, make_db

from emailtomcp.core.clock import FakeClock
from emailtomcp.core.errors import AuthError, PermanentError, TransientError
from emailtomcp.core.events import EventBus
from emailtomcp.mail import send_service as send_service_module
from emailtomcp.mail.draft_service import DraftService
from emailtomcp.mail.send_service import EVENT_SEND_COMPLETED, EVENT_SEND_FAILED, SendService
from emailtomcp.mail.sync_service import EVENT_ACCOUNT_CONNECTED, EVENT_ACCOUNT_ERROR
from emailtomcp.storage.repositories import drafts as drafts_repo


class _DummyCredentialProvider:
    def get_password(self, account_id: int) -> str:
        return "dummy-password"


def _factory(_auth_method: str) -> _DummyCredentialProvider:
    return _DummyCredentialProvider()


def _setup(tmp_path: Path):
    db = make_db(tmp_path)
    mail_dir = tmp_path / "mail"
    mail_dir.mkdir()
    account_id = make_account(db, incoming_protocol="pop3", in_port=995, out_port=465)
    draft_service = DraftService(db)
    draft_id = draft_service.create_draft(
        account_id=account_id,
        to_addrs=["dest@example.com"],
        subject="제목",
        body_text="본문",
    )
    drafts_repo.mark_outbox(
        db, draft_id, message_id_hdr="<test@example.com>", outbox_expires_at=None
    )
    clock = FakeClock()
    service = SendService(
        db, mail_blob_dir=mail_dir, credential_provider_factory=_factory, clock=clock
    )
    return db, draft_id, service, clock


def test_send_now_success_marks_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, draft_id, service, _clock = _setup(tmp_path)
    try:
        monkeypatch.setattr(send_service_module.smtp_outgoing, "send_mail", lambda **kwargs: None)

        result = service.send_now(draft_id)
        assert result == "sent"

        conn = db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.status == "sent"
        assert draft.sent_message_id is not None  # 로컬 보낸편지함 사본이 생겼다
    finally:
        db.stop()


def test_permanent_error_fails_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, draft_id, service, _clock = _setup(tmp_path)
    try:

        def _raise(**kwargs):
            raise PermanentError("영구 오류")

        monkeypatch.setattr(send_service_module.smtp_outgoing, "send_mail", _raise)

        result = service.send_now(draft_id)
        assert result == "failed"

        conn = db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.status == "failed"
        assert draft.send_attempts == 0  # 재시도 집계 없이 즉시 실패
    finally:
        db.stop()


def test_auth_error_fails_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, draft_id, service, _clock = _setup(tmp_path)
    try:

        def _raise(**kwargs):
            raise AuthError("인증 실패")

        monkeypatch.setattr(send_service_module.smtp_outgoing, "send_mail", _raise)

        result = service.send_now(draft_id)
        assert result == "failed(auth)"

        conn = db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.status == "failed"
    finally:
        db.stop()


def test_transient_error_retries_then_fails_after_max_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, draft_id, service, clock = _setup(tmp_path)
    try:

        def _raise(**kwargs):
            raise TransientError("일시 오류")

        monkeypatch.setattr(send_service_module.smtp_outgoing, "send_mail", _raise)

        # 1차 시도: outbox -> sending -> outbox(재시도 대기), send_attempts=1
        result = service.send_now(draft_id)
        assert result == "retry_or_failed"
        conn = db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.status == "outbox"
        assert draft.send_attempts == 1

        # 시간을 흘려보내고 2차, 3차 재시도 — 세 번째에 최종 실패로 전이된다.
        clock.advance(600)
        service.send_now(draft_id)
        clock.advance(600)
        result3 = service.send_now(draft_id)
        assert result3 == "retry_or_failed"

        conn = db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.status == "failed"
        # send_attempts는 "재시도로 되돌린 횟수"다 — 3번째 시도는 재시도가 아니라
        # 바로 failed로 전이하므로 카운터는 2에서 멈춘다(총 시도는 3회).
        assert draft.send_attempts == 2
    finally:
        db.stop()


def test_recover_on_startup_marks_sending_as_send_unknown(tmp_path: Path) -> None:
    db, draft_id, service, _clock = _setup(tmp_path)
    try:
        drafts_repo.mark_sending(db, draft_id)  # 강제종료 시뮬레이션: sending 상태로 남음

        service.recover_on_startup()

        conn = db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        assert draft is not None
        assert draft.status == "send_unknown"  # 자동 재발송 금지(§5.3)
    finally:
        db.stop()


def test_send_now_success_publishes_connected_and_completed_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, draft_id, _service, clock = _setup(tmp_path)
    try:
        monkeypatch.setattr(send_service_module.smtp_outgoing, "send_mail", lambda **kwargs: None)
        bus = EventBus()
        connected: list[dict] = []
        completed: list[dict] = []
        bus.subscribe(EVENT_ACCOUNT_CONNECTED, lambda name, payload: connected.append(payload))
        bus.subscribe(EVENT_SEND_COMPLETED, lambda name, payload: completed.append(payload))
        mail_dir = tmp_path / "mail"
        service = SendService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=_factory,
            clock=clock,
            event_bus=bus,
        )

        result = service.send_now(draft_id)

        assert result == "sent"
        assert len(connected) == 1
        assert len(completed) == 1 and completed[0]["draft_id"] == draft_id
    finally:
        db.stop()


def test_send_now_permanent_failure_publishes_error_and_failed_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, draft_id, _service, clock = _setup(tmp_path)
    try:

        def _raise(**kwargs):
            raise PermanentError("영구 오류")

        monkeypatch.setattr(send_service_module.smtp_outgoing, "send_mail", _raise)
        bus = EventBus()
        errors: list[dict] = []
        failed: list[dict] = []
        bus.subscribe(EVENT_ACCOUNT_ERROR, lambda name, payload: errors.append(payload))
        bus.subscribe(EVENT_SEND_FAILED, lambda name, payload: failed.append(payload))
        mail_dir = tmp_path / "mail"
        service = SendService(
            db,
            mail_blob_dir=mail_dir,
            credential_provider_factory=_factory,
            clock=clock,
            event_bus=bus,
        )

        result = service.send_now(draft_id)

        assert result == "failed"
        assert len(errors) == 1
        assert len(failed) == 1 and failed[0]["draft_id"] == draft_id
    finally:
        db.stop()


def test_send_now_resolves_local_file_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """작성 창에서 사용자가 드래그앤드롭으로 첨부한 로컬 파일(§11.2)이 실제 발신
    MIME에 포함되는지 확인한다(`DraftService.stage_local_attachment` 산출물 소비)."""
    db, _draft_id, service, _clock = _setup(tmp_path)
    try:
        from emailtomcp.storage.repositories import accounts as accounts_repo

        conn = db.new_read_connection()
        try:
            account_id = accounts_repo.list_accounts(conn)[0].id
        finally:
            conn.close()

        mail_dir = tmp_path / "mail"
        draft_service = DraftService(db, mail_blob_dir=mail_dir)
        source_file = tmp_path / "report.txt"
        source_file.write_bytes(b"local attachment payload")
        meta = draft_service.stage_local_attachment(str(source_file))

        new_draft_id = draft_service.create_draft(
            account_id=account_id,
            to_addrs=["dest@example.com"],
            subject="첨부 테스트",
            body_text="본문",
            attachments=[meta],
        )
        drafts_repo.mark_outbox(
            db, new_draft_id, message_id_hdr="<attach-test@example.com>", outbox_expires_at=None
        )

        captured: dict = {}

        def _capture_send(**kwargs):
            captured["raw_bytes"] = kwargs["raw_bytes"]

        monkeypatch.setattr(send_service_module.smtp_outgoing, "send_mail", _capture_send)

        result = service.send_now(new_draft_id)

        assert result == "sent"
        assert b"report.txt" in captured["raw_bytes"]
    finally:
        db.stop()


def test_process_outbox_isolates_failures_across_drafts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """초안 하나의 처리 실패가 `process_outbox`의 나머지 결과 집계를 막지 않는다."""
    db, draft_id, service, _clock = _setup(tmp_path)
    try:

        def _raise(**kwargs):
            raise RuntimeError("예상 못한 오류")

        monkeypatch.setattr(send_service_module.smtp_outgoing, "send_mail", _raise)
        results = service.process_outbox()
        assert draft_id in results
        assert "error" in results[draft_id]
    finally:
        db.stop()
