"""토큰 저장소(§6.5, H4)와 MCP 레이트리밋(§7.7) 단위 테스트."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from emailtomcp.core.clock import FakeClock
from emailtomcp.core.errors import PolicyError
from emailtomcp.core.events import EventBus
from emailtomcp.mail.draft_service import DraftService
from emailtomcp.mcp_server.approval import (
    MAX_REQUESTS_PER_MINUTE,
    TOKEN_REJECT_BACKOFF_BASE,
    ApprovalService,
    snapshot_hash_of,
)
from emailtomcp.mcp_server.auth import (
    SECRET_SERVICE,
    TokenStore,
    ensure_handshake_secret,
    hash_token,
    proxy_token_key,
    read_handshake_secret,
)
from emailtomcp.rules import rate_limit
from emailtomcp.storage.repositories import drafts as drafts_repo
from mcp_support import MemorySecretStore, make_account, make_db

pytestmark = pytest.mark.unit


def _store(tmp_path: Path) -> tuple[TokenStore, MemorySecretStore, FakeClock, object]:
    db = make_db(tmp_path)
    secrets = MemorySecretStore()
    clock = FakeClock(datetime.now(UTC))
    return TokenStore(db, secrets, clock), secrets, clock, db


def test_http_token_plaintext_returned_once_and_only_hash_stored(tmp_path: Path) -> None:
    store, _secrets, _clock, db = _store(tmp_path)
    try:
        issued = store.issue(kind="http", scopes=["read"], label="laptop")
        assert issued.plaintext and len(issued.plaintext) >= 40
        conn = db.new_read_connection()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT * FROM mcp_tokens WHERE id = ?", (issued.token_id,)
            ).fetchone()
            dump = " ".join(str(v) for v in tuple(row))
        finally:
            conn.close()
        assert issued.plaintext not in dump
        assert row["token_sha256"] == hash_token(issued.plaintext)
        principal = store.authenticate(issued.plaintext)
        assert principal is not None
        assert principal.scopes == frozenset({"read"})
        assert principal.token_hash8 == hash_token(issued.plaintext)[:8]
        assert store.authenticate(issued.plaintext + "x") is None
        assert store.authenticate("") is None
        assert store.authenticate("a" * 1000) is None
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_proxy_token_lives_in_keyring_and_reissue_revokes_previous(tmp_path: Path) -> None:
    store, secrets, _clock, db = _store(tmp_path)
    try:
        first = store.issue(kind="proxy", scopes=["read", "draft"], client="default")
        assert first.plaintext is None
        token1 = secrets.get(SECRET_SERVICE, proxy_token_key("default"))
        assert token1 and store.authenticate(token1) is not None
        second = store.issue(kind="proxy", scopes=["read"], client="default")
        token2 = secrets.get(SECRET_SERVICE, proxy_token_key("default"))
        assert token2 != token1
        assert store.authenticate(token1) is None  # 이전 토큰 자동 폐기
        assert store.authenticate(token2) is not None
        assert store.revoke(second.token_id)
        assert store.authenticate(token2) is None
        assert secrets.get(SECRET_SERVICE, proxy_token_key("default")) is None
        with pytest.raises(ValueError):
            store.issue(kind="proxy", scopes=["read"], client="bad name!")
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_token_expiry_default_180_days(tmp_path: Path) -> None:
    store, _secrets, clock, db = _store(tmp_path)
    try:
        issued = store.issue(kind="http", scopes=["read"], label="t")
        assert issued.plaintext
        clock.advance(timedelta(days=179).total_seconds())
        assert store.authenticate(issued.plaintext) is not None
        clock.advance(timedelta(days=2).total_seconds())
        assert store.authenticate(issued.plaintext) is None
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_handshake_secret_created_once() -> None:
    secrets = MemorySecretStore()
    assert read_handshake_secret(secrets) is None
    first = ensure_handshake_secret(secrets)
    assert len(first) == 32
    assert ensure_handshake_secret(secrets) == first
    assert read_handshake_secret(secrets) == first


def test_read_handshake_secret_rejects_invalid_hex() -> None:
    """keyring 값이 손상돼 유효한 hex가 아니면 None(앱을 처음 실행한 것처럼 취급)."""
    secrets = MemorySecretStore()
    secrets.set(SECRET_SERVICE, "mcp:handshake_secret", "not-valid-hex!!")
    assert read_handshake_secret(secrets) is None


def test_issue_rejects_invalid_kind_ttl_and_label(tmp_path: Path) -> None:
    store, _secrets, _clock, db = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="알 수 없는 토큰 종류"):
            store.issue(kind="bogus", scopes=["read"])
        with pytest.raises(ValueError, match="만료 기간"):
            store.issue(kind="http", scopes=["read"], label="t", ttl_days=0)
        with pytest.raises(ValueError, match="만료 기간"):
            store.issue(kind="http", scopes=["read"], label="t", ttl_days=3651)
        with pytest.raises(ValueError, match="토큰 라벨"):
            store.issue(kind="http", scopes=["read"], label="   ")
        with pytest.raises(ValueError, match="토큰 라벨"):
            store.issue(kind="http", scopes=["read"], label="x" * 101)
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_issue_proxy_token_rolls_back_when_keyring_set_fails(tmp_path: Path) -> None:
    """keyring에 평문을 못 넣으면 방금 만든 토큰을 즉시 폐기한다(평문을 어디에도 남기지 않음)."""
    store, secrets, _clock, db = _store(tmp_path)
    try:

        def _boom(_service: str, _key: str, _value: str) -> None:
            raise RuntimeError("keyring unavailable")

        secrets.set = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="keyring unavailable"):
            store.issue(kind="proxy", scopes=["read"], client="default")
        tokens = store.list_tokens()
        assert len(tokens) == 1 and tokens[0].revoked_at is not None
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_revoke_unknown_token_returns_false(tmp_path: Path) -> None:
    store, _secrets, _clock, db = _store(tmp_path)
    try:
        assert store.revoke(999_999) is False
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_revoke_proxy_token_with_mismatched_keyring_value_keeps_keyring(tmp_path: Path) -> None:
    """keyring 값이 이 토큰 것과 다르면(예: 이미 재발급됨) 폐기는 성공하지만 keyring은 안 지운다."""
    store, secrets, _clock, db = _store(tmp_path)
    try:
        issued = store.issue(kind="proxy", scopes=["read"], client="default")
        secrets.set(SECRET_SERVICE, proxy_token_key("default"), "someone-elses-value")
        assert store.revoke(issued.token_id) is True
        assert secrets.get(SECRET_SERVICE, proxy_token_key("default")) == "someone-elses-value"
    finally:
        db.stop()  # type: ignore[attr-defined]


def test_authenticate_touch_tolerates_bad_last_used_at_and_touch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """마지막 사용 시각 파싱 실패·갱신 실패는 인증 자체를 막지 않는다."""
    store, _secrets, clock, db = _store(tmp_path)
    try:
        issued = store.issue(kind="http", scopes=["read"], label="t")
        assert issued.plaintext
        # last_used_at을 깨진 값으로 직접 손상시킨다.
        conn = db.new_read_connection()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?",
                ("not-a-date", issued.token_id),
            )
            conn.commit()
        finally:
            conn.close()
        assert store.authenticate(issued.plaintext) is not None  # ValueError를 삼키고 계속 진행

        import emailtomcp.storage.repositories.mcp as mcp_repo

        def _boom(*_a: object, **_kw: object) -> None:
            raise RuntimeError("writer busy")

        monkeypatch.setattr(mcp_repo, "touch_token", _boom)
        clock.advance(120)
        assert store.authenticate(issued.plaintext) is not None  # 갱신 실패도 인증을 막지 않음
    finally:
        db.stop()  # type: ignore[attr-defined]


def _sent_via_mcp(db: object, account_id: int, approved_at: datetime) -> None:
    draft_id = DraftService(db).create_draft(  # type: ignore[arg-type]
        account_id=account_id, to_addrs=["x@example.org"], subject="s", body_text="b", origin="mcp"
    )
    ok = drafts_repo.approve_to_outbox(
        db,  # type: ignore[arg-type]
        draft_id,
        from_states=["draft"],
        verify=lambda _c, _d: None,
        approved_by="policy_allowlist",
        approved_at=approved_at.isoformat(),
        message_id_hdr=f"<{draft_id}@t>",
    )
    assert ok


def test_rate_limit_hourly_boundary_20_allowed_21_denied(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        now = datetime.now(UTC)
        for _ in range(19):
            _sent_via_mcp(db, account_id, now - timedelta(minutes=5))
        conn = db.new_read_connection()
        try:
            rate_limit.check_mcp_send_limit(conn, now)  # 20건째: 허용
        finally:
            conn.close()
        _sent_via_mcp(db, account_id, now - timedelta(minutes=1))
        conn = db.new_read_connection()
        try:
            assert rate_limit.mcp_send_usage(conn, now) == (20, 20)
            with pytest.raises(PolicyError):
                rate_limit.check_mcp_send_limit(conn, now)  # 21건째: 거부
            # 1시간이 지나면 다시 허용(창 밖으로 빠짐)
            rate_limit.check_mcp_send_limit(conn, now + timedelta(hours=1, minutes=1))
        finally:
            conn.close()
    finally:
        db.stop()


def test_rate_limit_daily_cap_and_user_sends_not_counted(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        now = datetime.now(UTC)
        conn = db.new_read_connection()
        try:
            for i in range(100):
                _sent_via_mcp(db, account_id, now - timedelta(hours=2, minutes=i))
            with pytest.raises(PolicyError):
                rate_limit.check_mcp_send_limit(conn, now)
        finally:
            conn.close()
        # 사용자가 작성 창에서 직접 보낸 메일(user_send)은 MCP 상한에 포함하지 않는다.
        (tmp_path / "x").mkdir()
        db2 = make_db(tmp_path / "x")
        try:
            acc2 = make_account(db2)
            for _ in range(30):
                did = DraftService(db2).create_draft(
                    account_id=acc2, to_addrs=["y@example.org"], subject="s", body_text="b"
                )
                assert drafts_repo.mark_outbox(
                    db2,
                    did,
                    message_id_hdr=f"<u{did}@t>",
                    outbox_expires_at=None,
                    from_states=["draft"],
                    approved_by="user_send",
                )
            conn2 = db2.new_read_connection()
            try:
                rate_limit.check_mcp_send_limit(conn2, datetime.now(UTC))
            finally:
                conn2.close()
        finally:
            db2.stop()
    finally:
        db.stop()


# ---------------------------------------------------------------- 승인 요청 폭주 방지(보안검토 M-1)


def _issue_tokens(db: object, clock: FakeClock, count: int) -> list[int]:
    store = TokenStore(db, MemorySecretStore(), clock)  # type: ignore[arg-type]
    return [
        store.issue(kind="http", scopes=["read", "draft", "send"], label=f"t{i}").token_id
        for i in range(count)
    ]


def _mcp_draft(db: object, account_id: int, token_id: int) -> int:
    draft_id = DraftService(db).create_draft(  # type: ignore[arg-type]
        account_id=account_id, to_addrs=["x@example.org"], subject="s", body_text="b", origin="mcp"
    )
    drafts_repo.set_mcp_token_id(db, draft_id, token_id)  # type: ignore[arg-type]
    return draft_id


def test_approval_request_rate_per_minute_and_reject_cooldown(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        clock = FakeClock(datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
        approval = ApprovalService(db, EventBus(), clock)  # type: ignore[arg-type]
        tok1, tok2 = _issue_tokens(db, clock, 2)

        # 분당 상한: 대기 건수 상한(3건)에 걸리지 않게 매번 [수정 후 발송]으로 되돌린다.
        for _ in range(MAX_REQUESTS_PER_MINUTE):
            draft_id = _mcp_draft(db, account_id, token_id=tok1)
            approval.request(draft_id)
            assert approval.cancel_for_edit(draft_id)
        extra = _mcp_draft(db, account_id, token_id=tok1)
        with pytest.raises(PolicyError, match="너무 잦습니다"):
            approval.request(extra)
        # 다른 토큰(연결)은 영향받지 않는다.
        other = _mcp_draft(db, account_id, token_id=tok2)
        approval.request(other)
        assert approval.cancel_for_edit(other)
        clock.advance(61)
        approval.request(extra)  # 1분이 지나면 다시 허용

        # 거부 직후 재요청 쿨다운(60초)
        assert approval.reject(extra)
        with pytest.raises(PolicyError, match="거부"):
            approval.request(extra)
        conn = db.new_read_connection()
        try:
            assert drafts_repo.get_draft(conn, extra).status == "draft"  # type: ignore[union-attr]
        finally:
            conn.close()
        clock.advance(61)
        approval.request(extra)
    finally:
        db.stop()


def test_approval_token_reject_backoff_escalates_and_resets_on_approve(tmp_path: Path) -> None:
    """N-7: 거부가 반복되면(2회째부터) 토큰 단위로 지수 백오프가 걸리고, 새 초안을
    만들어도 같은 토큰이면 피해갈 수 없다. 승인을 받으면 연속 거부 횟수가 초기화된다."""
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        clock = FakeClock(datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
        approval = ApprovalService(db, EventBus(), clock)  # type: ignore[arg-type]
        (tok1,) = _issue_tokens(db, clock, 1)

        # 1회째 거부: 초안 단위(60초) 쿨다운만 걸리고, 토큰 단위 백오프는 아직 없다.
        first = _mcp_draft(db, account_id, token_id=tok1)
        approval.request(first)
        assert approval.reject(first)
        clock.advance(61)
        approval.request(first)  # 아직 토큰 백오프 없음 — 성공

        # 2회째 거부(같은 토큰, 다른 초안이어도 상관없음): 토큰 단위 백오프 시작(5분).
        assert approval.reject(first)
        second = _mcp_draft(db, account_id, token_id=tok1)
        with pytest.raises(PolicyError, match="거부가 반복"):
            approval.request(second)  # 새 초안을 만들어도 토큰이 같아서 막힌다

        # 백오프가 끝나기 전에는 여전히 거부된다.
        clock.advance(int(TOKEN_REJECT_BACKOFF_BASE.total_seconds()) - 5)
        with pytest.raises(PolicyError, match="거부가 반복"):
            approval.request(second)

        # 5분이 지나면 다시 허용된다.
        clock.advance(10)
        approval.request(second)

        # 승인을 받으면 그 토큰의 연속 거부 횟수가 초기화된다.
        conn = db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, second)
        finally:
            conn.close()
        outcome = approval.approve(second, snapshot_hash_of(draft))  # type: ignore[arg-type]
        assert outcome.ok

        third = _mcp_draft(db, account_id, token_id=tok1)
        approval.request(third)
        assert approval.reject(third)  # 초기화 이후 1회째 거부 — 아직 토큰 백오프 없음
        clock.advance(61)
        approval.request(third)
    finally:
        db.stop()


def test_approval_pending_cap_counts_per_token(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        clock = FakeClock()
        approval = ApprovalService(db, EventBus(), clock)  # type: ignore[arg-type]
        tok1, tok2 = _issue_tokens(db, clock, 2)
        for _ in range(3):
            approval.request(_mcp_draft(db, account_id, token_id=tok1))
        with pytest.raises(PolicyError, match="최대 3건"):
            approval.request(_mcp_draft(db, account_id, token_id=tok1))
        approval.request(_mcp_draft(db, account_id, token_id=tok2))  # 다른 토큰은 별도 집계
    finally:
        db.stop()
