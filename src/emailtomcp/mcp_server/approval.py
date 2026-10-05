"""MCP 발송 승인 (DESIGN.md §6.2, §5.3, §11.6, H9, C-04).

흐름(confirm 모드)
1. `send_draft` → `request()`가 writer 스레드 안에서 초안 행을 읽어 스냅샷 해시를
   계산하고, 같은 트랜잭션에서 `draft → pending_approval`로 바꾸며 `approval_hash`를
   저장한다(`drafts_repo.request_approval`). 그다음 `ApprovalRequested`를 발행한다.
2. UI의 승인 다이얼로그가 초안을 보여 주고, 사용자가 [승인]하면 `approve()`가
   writer 스레드 안에서 다시 스냅샷 해시를 계산해 **(a) 저장된 approval_hash,
   (b) 다이얼로그가 보여 준 내용의 해시** 둘 다와 일치할 때만 `pending_approval →
   outbox`로 바꾼다. 하나라도 다르면 승인은 무효이고 초안은 `draft`로 돌아간다.
3. 결과는 `ApprovalResolved`로 발행되고, 기다리던 `send_draft` 도구 호출이 깨어난다.
   120초 안에 결정이 없으면 도구는 "승인 대기 중"을 돌려주고 초안은 `pending_approval`
   으로 남는다. 24시간이 지나면 `expire_due()`가 `draft`로 되돌린다(C-04).

`pending_approval` 상태에서는 `update_draft`/UI 편집이 SQL 조건(`status='draft'`)으로
거부되므로, 승인 대기 중 내용이 바뀌는 TOCTOU는 생기지 않는다. 해시 검사는 그 위의
이중 방어다.

승인은 UI에서만 할 수 있다 — 이 모듈에는 MCP 도구로 노출되는 승인 경로가 없다(H9-5).

보안검토 P2 반영(2026-10-05)
- allowlist 정책 발송(`send_by_policy`)은 writer 안에서 읽은 초안 행으로 정책을 다시
  판정한다(H-2). 재판정 콜백은 필수 인자다.
- 승인 요청(`request`)은 토큰별 동시 대기 3건·분당 5건·거부 후 60초 쿨다운 상한을 writer
  안에서 검사한다(M-1).
- 거부 쿨다운은 초안 단위(60초)만으로는 새 초안을 만들면 바로 무력화되므로, 같은 MCP
  토큰이 거부를 반복하면(2회째부터) 지수 백오프로 그 토큰의 새 승인 요청 자체를 막는다
  (N-7). 토큰이 승인을 받으면 그 토큰의 거부 연속 횟수는 초기화된다.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import make_msgid
from enum import StrEnum
from typing import TYPE_CHECKING

from emailtomcp.core.errors import PolicyError
from emailtomcp.core.models import ApprovedBy, DraftStatus
from emailtomcp.mail.draft_service import APPROVAL_EXPIRY, compute_snapshot_hash
from emailtomcp.rules import rate_limit
from emailtomcp.storage.repositories import drafts as drafts_repo

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from emailtomcp.core.clock import Clock
    from emailtomcp.core.events import EventBus
    from emailtomcp.storage.db import Database
    from emailtomcp.storage.repositories.drafts import DraftRow

logger = logging.getLogger(__name__)

EVENT_APPROVAL_REQUESTED = "ApprovalRequested"
EVENT_APPROVAL_RESOLVED = "ApprovalResolved"
EVENT_DRAFT_CHANGED = "DraftChanged"

APPROVAL_WAIT_SECONDS = 120.0

# 승인 요청 폭주 방지(보안검토 M-1). "연결"은 MCP 토큰 단위다.
MAX_PENDING_PER_TOKEN = 3
MAX_REQUESTS_PER_MINUTE = 5
REQUEST_WINDOW = timedelta(minutes=1)
REJECT_COOLDOWN = timedelta(seconds=60)

# 토큰 단위 거부 쿨다운(지수 백오프, N-7) — 2회째 거부부터 적용된다.
# 5분 → 10분 → 20분 ... 최대 24시간. 승인을 받으면 연속 거부 횟수가 초기화된다.
TOKEN_REJECT_BACKOFF_BASE = timedelta(minutes=5)
TOKEN_REJECT_BACKOFF_MAX = timedelta(hours=24)


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EDIT = "edit"  # [수정 후 발송] — 사용자가 작성 창에서 직접 보낸다(새 스냅샷)
    EXPIRED = "expired"
    INVALIDATED = "invalidated"  # 스냅샷 해시 불일치


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    ok: bool
    decision: str | None
    message: str


class _HashMismatch(Exception):
    pass


class _Expired(Exception):
    pass


def snapshot_hash_of(draft: DraftRow) -> str:
    """초안 행의 현재 내용으로 스냅샷 해시를 계산한다(draft_service의 함수 재사용)."""
    return compute_snapshot_hash(
        to_addrs=draft.to_addrs,
        cc_addrs=draft.cc_addrs,
        bcc_addrs=draft.bcc_addrs,
        subject=draft.subject,
        body_text=draft.body_text,
        attachments=draft.attachments,
    )


class ApprovalService:
    def __init__(
        self,
        db: Database,
        event_bus: EventBus,
        clock: Clock,
        *,
        rate_check: Callable[[sqlite3.Connection, datetime], None] | None = None,
    ) -> None:
        self._db = db
        self._event_bus = event_bus
        self._clock = clock
        self._rate_check = rate_check or rate_limit.check_mcp_send_limit
        self._lock = threading.Lock()
        self._waiters: dict[int, list[tuple[asyncio.AbstractEventLoop, asyncio.Future[str]]]] = {}
        # 승인 요청 폭주 방지(M-1) — 토큰별 최근 요청 시각, 초안별 마지막 거부 시각.
        self._request_times: dict[int | None, deque[datetime]] = {}
        self._rejected_at: dict[int, datetime] = {}
        # 토큰 단위 거부 쿨다운(N-7) — 연속 거부 횟수와 다음 요청 가능 시각.
        self._token_reject_count: dict[int | None, int] = {}
        self._token_cooldown_until: dict[int | None, datetime] = {}
        event_bus.subscribe(EVENT_APPROVAL_RESOLVED, self._on_resolved)

    # ------------------------------------------------------------------ MCP 쪽

    def register_waiter(self, draft_id: int) -> asyncio.Future[str]:
        """`request()`보다 먼저 호출해야 한다(요청 직후 바로 결정이 나도 놓치지 않게)."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        with self._lock:
            self._waiters.setdefault(draft_id, []).append((loop, future))
        return future

    def unregister_waiter(self, draft_id: int, future: asyncio.Future[str]) -> None:
        with self._lock:
            entries = self._waiters.get(draft_id, [])
            self._waiters[draft_id] = [e for e in entries if e[1] is not future]
            if not self._waiters[draft_id]:
                del self._waiters[draft_id]

    async def wait(self, future: asyncio.Future[str], timeout: float) -> str | None:
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            return None

    def request(self, draft_id: int, owner_check: Callable[[DraftRow], None] | None = None) -> str:
        """`draft → pending_approval`, 스냅샷 해시 저장, `ApprovalRequested` 발행(블로킹).

        승인 요청 폭주(승인 피로·UI DoS, 보안검토 M-1)를 막는 상한을 writer 트랜잭션 안에서
        검사한다 — 동시에 여러 요청이 와도 직렬화되어 상한을 넘길 수 없다. 위반이면
        `PolicyError`이고 초안은 `draft` 상태 그대로 남는다.
        """
        now = self._clock.now()
        expires_at = now + APPROVAL_EXPIRY
        snapshot = drafts_repo.request_approval(
            self._db,
            draft_id,
            compute_hash=snapshot_hash_of,
            requested_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            verify=lambda conn, draft: self._verify_request(conn, draft, now, owner_check),
        )
        if snapshot is None:
            raise PolicyError("초안이 작성 중(draft) 상태가 아니라서 승인을 요청할 수 없습니다")
        logger.info("MCP 발송 승인 요청: draft_id=%s", draft_id)
        self._event_bus.publish(
            EVENT_APPROVAL_REQUESTED,
            {"draft_id": draft_id, "at": now.isoformat(), "expires_at": expires_at.isoformat()},
        )
        self._publish_draft_changed(draft_id)
        return snapshot

    def send_by_policy(self, draft_id: int, policy_check: Callable[[DraftRow], None]) -> bool:
        """allowlist 정책 통과 시 `draft → outbox`(승인 주체 policy_allowlist).

        `policy_check(draft)`는 **writer 트랜잭션이 방금 읽은 초안 행**으로 allowlist·첨부
        조건을 다시 판정하는 콜백이다(보안검토 H-2). 호출자의 사전 판정은 읽기 스냅샷
        기준이라, 판정과 전이 사이에 `update_draft`로 수신자를 끼워 넣을 수 있기 때문이다.
        writer는 단일 스레드이므로 이 판정과 상태 전이 사이에는 다른 쓰기가 끼어들 수 없다.
        위반이면 콜백이 `PolicyError`를 던지고 아무것도 바뀌지 않는다.

        레이트리밋도 같은 writer 트랜잭션 안에서 검사한다. 위반이면 `PolicyError`.
        """
        now = self._clock.now()

        def _verify(conn: sqlite3.Connection, draft: DraftRow) -> None:
            # 필수 인자다 — 재판정 없이 정책 발송하는 경로를 만들 수 없게(fail-closed).
            policy_check(draft)
            self._rate_check(conn, now)

        ok = drafts_repo.approve_to_outbox(
            self._db,
            draft_id,
            from_states=[DraftStatus.DRAFT],
            verify=_verify,
            approved_by=ApprovedBy.POLICY_ALLOWLIST,
            approved_at=now.isoformat(),
            message_id_hdr=make_msgid(),
        )
        if ok:
            logger.info("MCP 발송 allowlist 정책 통과: draft_id=%s", draft_id)
            self._publish_draft_changed(draft_id)
        return ok

    # ------------------------------------------------------------------ UI 쪽(블로킹)

    def approve(self, draft_id: int, expected_hash: str) -> ApprovalOutcome:
        """UI [승인]. `expected_hash`는 다이얼로그가 보여 준 내용의 스냅샷 해시다."""
        now = self._clock.now()
        token_id_holder: list[int | None] = []

        def _verify(conn: sqlite3.Connection, draft: DraftRow) -> None:
            token_id_holder.append(draft.mcp_token_id)
            stored = draft.approval_hash or ""
            current = snapshot_hash_of(draft)
            if not (
                stored
                and hmac.compare_digest(stored, expected_hash or "")
                and hmac.compare_digest(stored, current)
            ):
                raise _HashMismatch
            if (
                draft.approval_expires_at
                and datetime.fromisoformat(draft.approval_expires_at) <= now
            ):
                raise _Expired
            self._rate_check(conn, now)

        try:
            ok = drafts_repo.approve_to_outbox(
                self._db,
                draft_id,
                from_states=[DraftStatus.PENDING_APPROVAL],
                verify=_verify,
                approved_by=ApprovedBy.UI,
                approved_at=now.isoformat(),
                message_id_hdr=make_msgid(),
            )
        except _HashMismatch:
            logger.warning("승인 무효(스냅샷 해시 불일치): draft_id=%s", draft_id)
            self._resolve(draft_id, Decision.INVALIDATED, revert=True)
            return ApprovalOutcome(
                False,
                Decision.INVALIDATED,
                "승인 요청 이후 초안 내용이 바뀌어 승인이 무효 처리되었습니다. "
                "초안은 작성 중 상태로 돌아갔습니다.",
            )
        except _Expired:
            self._resolve(draft_id, Decision.EXPIRED, revert=True)
            return ApprovalOutcome(False, Decision.EXPIRED, "승인 대기 시간(24시간)이 지났습니다.")
        except PolicyError as exc:
            # 레이트리밋 등 — 초안은 승인 대기 상태로 남는다(나중에 다시 승인 가능).
            return ApprovalOutcome(False, None, str(exc))
        if not ok:
            return ApprovalOutcome(False, None, "이미 처리되었거나 승인 대기 상태가 아닙니다.")
        logger.info("MCP 발송 UI 승인: draft_id=%s", draft_id)
        # 승인을 받았으니 이 토큰의 거부 연속 횟수(N-7 백오프)를 초기화한다.
        token_id = token_id_holder[0] if token_id_holder else None
        with self._lock:
            self._token_reject_count.pop(token_id, None)
            self._token_cooldown_until.pop(token_id, None)
        self._resolve(draft_id, Decision.APPROVED, revert=False)
        return ApprovalOutcome(True, Decision.APPROVED, "승인했습니다. 보낼편지함에서 발송됩니다.")

    def reject(self, draft_id: int) -> bool:
        token_id = self._mcp_token_id_of(draft_id)
        changed = self._resolve(draft_id, Decision.REJECTED, revert=True)
        if changed:
            now = self._clock.now()
            with self._lock:
                # 거부 직후 같은 초안으로 다시 승인 요청을 띄우지 못하게 한다(M-1 쿨다운).
                self._rejected_at[draft_id] = now
                # 같은 토큰이 거부를 반복하면 2회째부터 지수 백오프로 전체 재요청을
                # 막는다(N-7) — 초안 단위 쿨다운은 새 초안을 만들면 바로 무력화된다.
                count = self._token_reject_count.get(token_id, 0) + 1
                self._token_reject_count[token_id] = count
                if count >= 2:
                    backoff = min(
                        TOKEN_REJECT_BACKOFF_MAX,
                        TOKEN_REJECT_BACKOFF_BASE * (2 ** (count - 2)),
                    )
                    self._token_cooldown_until[token_id] = now + backoff
        return changed

    def _mcp_token_id_of(self, draft_id: int) -> int | None:
        conn = self._db.new_read_connection()
        try:
            draft = drafts_repo.get_draft(conn, draft_id)
        finally:
            conn.close()
        return draft.mcp_token_id if draft is not None else None

    def cancel_for_edit(self, draft_id: int) -> bool:
        """[수정 후 발송] — 승인 요청을 취소하고 초안을 사용자 편집 가능 상태로 돌린다.

        같은 트랜잭션에서 초안 소유권을 사용자에게 넘긴다(`origin='user'`, 보안검토 N-1) —
        사용자가 작성 창에서 확인하는 동안 그 초안을 만든 MCP 토큰이 `update_draft`로
        수신자를 바꿔 끼우지 못하게 한다(`require_mcp_owned` + writer 측 소유권 조건).
        """
        return self._resolve(draft_id, Decision.EDIT, revert=True, hand_over_to_user=True)

    def expire_due(self) -> int:
        """24시간 지난 승인 대기 초안을 `draft`로 되돌린다(백엔드 주기 작업, C-04)."""
        conn = self._db.new_read_connection()
        try:
            ids = drafts_repo.list_expired_approvals(conn, now_iso=self._clock.now().isoformat())
        finally:
            conn.close()
        count = 0
        for draft_id in ids:
            if self._resolve(draft_id, Decision.EXPIRED, revert=True):
                count += 1
        return count

    # ------------------------------------------------------------------ 내부

    def _verify_request(
        self,
        conn: sqlite3.Connection,
        draft: DraftRow,
        now: datetime,
        owner_check: Callable[[DraftRow], None] | None,
    ) -> None:
        # 소유권도 writer가 방금 읽은 행으로 다시 확인한다 — 호출자의 사전 확인 이후
        # 사용자가 초안을 넘겨받았을 수 있다(보안검토 N-1).
        if owner_check is not None:
            owner_check(draft)
        self._check_request_limits(conn, draft, now)

    def _check_request_limits(
        self, conn: sqlite3.Connection, draft: DraftRow, now: datetime
    ) -> None:
        """승인 요청 상한(M-1, N-7). writer 스레드 안에서 `request_approval`의 verify로
        호출된다.

        1. 토큰별 동시 승인 대기(`pending_approval`) 건수 < `MAX_PENDING_PER_TOKEN`
        2. 거부된 초안은 `REJECT_COOLDOWN` 동안 다시 요청할 수 없다.
        3. 같은 토큰이 거부를 반복하면(2회째부터) 지수 백오프 동안 그 토큰의 어떤 초안도
           새로 승인을 요청할 수 없다(N-7) — 2만으로는 새 초안을 만들면 바로 무력화된다.
        4. 토큰별 최근 1분 승인 요청 건수 < `MAX_REQUESTS_PER_MINUTE`

        2·3·4는 메모리 기준이다(앱을 재시작하면 초기화). 화면에 다이얼로그가 연달아 뜨는
        것을 막는 용도라 재시작을 넘어 유지할 필요는 없다. 1은 DB 기준이라 재시작 후에도
        유지된다. 판정 순서상 상한 위반 요청은 분당 카운트에 넣지 않는다.
        """
        token_id = draft.mcp_token_id
        pending = drafts_repo.count_pending_approvals(conn, mcp_token_id=token_id)
        if pending >= MAX_PENDING_PER_TOKEN:
            raise PolicyError(
                f"사용자 승인을 기다리는 초안이 이미 {pending}건 있습니다(연결당 최대 "
                f"{MAX_PENDING_PER_TOKEN}건). 기존 요청이 처리된 뒤 다시 시도하세요."
            )
        with self._lock:
            for key, at in list(self._rejected_at.items()):
                if now - at >= REJECT_COOLDOWN:
                    del self._rejected_at[key]
            rejected_at = self._rejected_at.get(draft.id)
            if rejected_at is not None:
                wait = int((REJECT_COOLDOWN - (now - rejected_at)).total_seconds()) + 1
                raise PolicyError(
                    "사용자가 방금 이 초안의 발송을 거부했습니다. "
                    f"{wait}초 뒤에 다시 요청할 수 있습니다(같은 요청을 반복하지 마세요)."
                )
            cooldown_until = self._token_cooldown_until.get(token_id)
            if cooldown_until is not None:
                if now < cooldown_until:
                    wait = int((cooldown_until - now).total_seconds()) + 1
                    raise PolicyError(
                        "이 연결에서 승인 거부가 반복되어 새 승인 요청을 잠시 막습니다. "
                        f"{wait}초 뒤에 다시 시도하세요."
                    )
                del self._token_cooldown_until[token_id]
            times = self._request_times.setdefault(token_id, deque())
            while times and now - times[0] >= REQUEST_WINDOW:
                times.popleft()
            if len(times) >= MAX_REQUESTS_PER_MINUTE:
                raise PolicyError(
                    f"승인 요청이 너무 잦습니다(연결당 1분에 최대 {MAX_REQUESTS_PER_MINUTE}건). "
                    "잠시 후 다시 시도하세요."
                )
            times.append(now)

    def _resolve(
        self, draft_id: int, decision: str, *, revert: bool, hand_over_to_user: bool = False
    ) -> bool:
        changed = (
            drafts_repo.revert_approval_to_draft(
                self._db, draft_id, hand_over_to_user=hand_over_to_user
            )
            if revert
            else True
        )
        if changed:
            self._event_bus.publish(
                EVENT_APPROVAL_RESOLVED,
                {
                    "draft_id": draft_id,
                    "decision": str(decision),
                    "at": self._clock.now().isoformat(),
                },
            )
            self._publish_draft_changed(draft_id)
        return changed

    def _publish_draft_changed(self, draft_id: int) -> None:
        self._event_bus.publish(EVENT_DRAFT_CHANGED, {"draft_id": draft_id})

    def _on_resolved(self, _name: str, payload: object) -> None:
        """`ApprovalResolved` 구독 — 기다리던 send_draft 호출을 스레드 안전하게 깨운다."""
        if not isinstance(payload, dict):
            return
        draft_id = payload.get("draft_id")
        decision = str(payload.get("decision", ""))
        if not isinstance(draft_id, int):
            return
        with self._lock:
            entries = list(self._waiters.get(draft_id, []))
        for loop, future in entries:

            def _set(fut: asyncio.Future[str] = future) -> None:
                if not fut.done():
                    fut.set_result(decision)

            # 루프가 이미 닫혔으면(종료 중) 깨울 대상이 없다.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_set)
