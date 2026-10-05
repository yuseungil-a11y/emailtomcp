"""`drafts`/`draft_recipients` 테이블 레포지토리 (DESIGN.md §5.1, §5.3 상태 전이).

상태 전이는 전부 `storage.transitions.transition()`을 거친다 — 이 모듈에서 직접
`UPDATE ... SET status = ...`를 하지 않는다(경합 시 한쪽만 성공해야 하기 때문).

의존 규칙(§4.2)상 storage는 core에만 의존한다 — 주소 정규화(`mail.addressing`)는
**호출자(mail 계층, draft_service)**가 끝내고, `addr_norm`/`domain_norm`은 이미
계산된 값으로 받는다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from emailtomcp.storage.transitions import transition

if TYPE_CHECKING:
    from emailtomcp.storage.db import Database


class RecipientNorm(NamedTuple):
    """`draft_recipients` 한 행(정규화는 호출자가 미리 끝낸다)."""

    kind: str  # "to" | "cc" | "bcc"
    addr_norm: str
    domain_norm: str


@dataclass(slots=True)
class DraftRow:
    id: int
    account_id: int
    kind: str
    source_message_id: int | None
    to_addrs: list[str]
    cc_addrs: list[str]
    bcc_addrs: list[str]
    subject: str | None
    body_text: str | None
    body_html: str | None
    attachments: list[dict]
    origin: str
    status: str
    approval_hash: str | None
    message_id_hdr: str | None
    send_attempts: int
    next_send_at: str | None
    sent_at: str | None
    sent_message_id: int | None
    last_error: str | None
    # P2(승인 플로우)에서 추가로 읽는 컬럼. 기존 생성 코드와의 호환을 위해 기본값을 둔다.
    approval_requested_at: str | None = None
    approval_expires_at: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    mcp_token_id: int | None = None
    updated_at: str | None = None


def _row_to_draft(row: sqlite3.Row) -> DraftRow:
    return DraftRow(
        id=row["id"],
        account_id=row["account_id"],
        kind=row["kind"],
        source_message_id=row["source_message_id"],
        to_addrs=json.loads(row["to_addrs"]) if row["to_addrs"] else [],
        cc_addrs=json.loads(row["cc_addrs"]) if row["cc_addrs"] else [],
        bcc_addrs=json.loads(row["bcc_addrs"]) if row["bcc_addrs"] else [],
        subject=row["subject"],
        body_text=row["body_text"],
        body_html=row["body_html"],
        attachments=json.loads(row["attachments_json"]) if row["attachments_json"] else [],
        origin=row["origin"],
        status=row["status"],
        approval_hash=row["approval_hash"],
        message_id_hdr=row["message_id_hdr"],
        send_attempts=row["send_attempts"],
        next_send_at=row["next_send_at"],
        sent_at=row["sent_at"],
        sent_message_id=row["sent_message_id"],
        last_error=row["last_error"],
        approval_requested_at=row["approval_requested_at"],
        approval_expires_at=row["approval_expires_at"],
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
        mcp_token_id=row["mcp_token_id"],
        updated_at=row["updated_at"],
    )


def get_draft(conn: sqlite3.Connection, draft_id: int) -> DraftRow | None:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return _row_to_draft(row) if row else None


def create_draft(
    db: Database,
    *,
    account_id: int,
    kind: str,
    origin: str,
    to_addrs: list[str],
    subject: str | None,
    body_text: str | None,
    recipient_norms: list[RecipientNorm],
    cc_addrs: list[str] | None = None,
    bcc_addrs: list[str] | None = None,
    body_html: str | None = None,
    attachments: list[dict] | None = None,
    source_message_id: int | None = None,
    job_id: int | None = None,
) -> int:
    """초안 하나를 만든다(`status='draft'`). `draft_recipients`도 같은 트랜잭션에 채운다.

    `recipient_norms`는 호출자(draft_service)가 `mail.addressing`으로 미리 정규화한
    (kind, addr_norm, domain_norm) 목록이다.
    """
    now = datetime.now(UTC).isoformat()
    cc_addrs = cc_addrs or []
    bcc_addrs = bcc_addrs or []

    def _write(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN")
        try:
            cursor = conn.execute(
                """
                INSERT INTO drafts (
                    account_id, kind, source_message_id, to_addrs, cc_addrs, bcc_addrs,
                    subject, body_text, body_html, attachments_json, origin, job_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    account_id,
                    kind,
                    source_message_id,
                    json.dumps(to_addrs, ensure_ascii=False),
                    json.dumps(cc_addrs, ensure_ascii=False),
                    json.dumps(bcc_addrs, ensure_ascii=False),
                    subject,
                    body_text,
                    body_html,
                    json.dumps(attachments or [], ensure_ascii=False),
                    origin,
                    job_id,
                    now,
                    now,
                ),
            )
            assert cursor.lastrowid is not None
            draft_id = cursor.lastrowid
            for item in recipient_norms:
                conn.execute(
                    "INSERT INTO draft_recipients (draft_id, kind, addr_norm, domain_norm) "
                    "VALUES (?, ?, ?, ?)",
                    (draft_id, item.kind, item.addr_norm, item.domain_norm),
                )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return draft_id

    return db.writer.submit(_write).result()


def update_compose(
    db: Database,
    draft_id: int,
    *,
    to_addrs: list[str],
    cc_addrs: list[str],
    bcc_addrs: list[str],
    subject: str | None,
    body_text: str | None,
    attachments: list[dict],
    recipient_norms: list[RecipientNorm],
    mcp_owner_token_id: int | None = None,
) -> bool:
    """작성 창의 자동저장/수동저장 공용 — `status='draft'`일 때만 갱신된다(§5.3).

    `mcp_owner_token_id`를 주면(MCP `update_draft` 경로) `origin='mcp'`이고 그 토큰이 만든
    초안일 때만 갱신한다. 소유권 확인을 writer 트랜잭션의 WHERE 조건으로 한 번 더 걸어,
    호출자의 읽기 스냅샷 확인 이후 사용자가 초안을 넘겨받은 경우(보안검토 N-1, origin이
    'user'로 바뀜)에도 MCP가 덮어쓰지 못하게 한다.
    """

    def _write(conn: sqlite3.Connection) -> bool:
        conn.execute("BEGIN")
        try:
            changed = _apply_compose_fields(
                conn,
                draft_id,
                to_addrs=to_addrs,
                cc_addrs=cc_addrs,
                bcc_addrs=bcc_addrs,
                subject=subject,
                body_text=body_text,
                attachments=attachments,
                recipient_norms=recipient_norms,
                mcp_owner_token_id=mcp_owner_token_id,
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return changed

    return db.writer.submit(_write).result()


def _apply_compose_fields(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    to_addrs: list[str],
    cc_addrs: list[str],
    bcc_addrs: list[str],
    subject: str | None,
    body_text: str | None,
    attachments: list[dict],
    recipient_norms: list[RecipientNorm],
    mcp_owner_token_id: int | None = None,
) -> bool:
    """열린 트랜잭션 안에서 작성 필드와 `draft_recipients`를 덮어쓴다(커밋은 호출자 몫).

    `status='draft'`가 아니거나(그리고 `mcp_owner_token_id`를 줬다면 소유 토큰이 다르면)
    아무것도 바꾸지 않고 False.
    """
    sql = (
        "UPDATE drafts SET to_addrs = ?, cc_addrs = ?, bcc_addrs = ?, subject = ?, "
        "body_text = ?, attachments_json = ?, updated_at = ? WHERE id = ? "
        "AND status = 'draft'"
    )
    params: list[object] = [
        json.dumps(to_addrs, ensure_ascii=False),
        json.dumps(cc_addrs, ensure_ascii=False),
        json.dumps(bcc_addrs, ensure_ascii=False),
        subject,
        body_text,
        json.dumps(attachments, ensure_ascii=False),
        datetime.now(UTC).isoformat(),
        draft_id,
    ]
    if mcp_owner_token_id is not None:
        sql += " AND origin = 'mcp' AND mcp_token_id = ?"
        params.append(mcp_owner_token_id)
    cursor = conn.execute(sql, params)
    if cursor.rowcount != 1:
        return False
    conn.execute("DELETE FROM draft_recipients WHERE draft_id = ?", (draft_id,))
    for item in recipient_norms:
        conn.execute(
            "INSERT INTO draft_recipients (draft_id, kind, addr_norm, domain_norm) "
            "VALUES (?, ?, ?, ?)",
            (draft_id, item.kind, item.addr_norm, item.domain_norm),
        )
    return True


def save_compose_and_mark_outbox(
    db: Database,
    draft_id: int,
    *,
    to_addrs: list[str],
    cc_addrs: list[str],
    bcc_addrs: list[str],
    subject: str | None,
    body_text: str | None,
    attachments: list[dict],
    recipient_norms: list[RecipientNorm],
    message_id_hdr: str,
    approved_by: str,
) -> bool:
    """작성 창 [보내기] 전용 — 화면 내용 저장과 `draft → outbox` 전이를 writer 작업 하나로
    처리한다(보안검토 N-1/R-1).

    예전에는 자동저장(writer 작업 1)과 Outbox 전이(writer 작업 2)가 따로 실행돼, 그 사이에
    MCP `update_draft`가 끼어들면 사용자가 화면에서 본 적 없는 수신자(예: 외부 BCC)가
    승인 없이 나갈 수 있었다. writer는 단일 스레드라 이 함수 안의 저장과 전이 사이에는
    다른 쓰기가 끼어들 수 없다 — 나가는 내용은 언제나 호출자(작성 창)가 넘긴 내용이다.

    Returns:
        전이에 성공하면 True. 초안이 없거나 `draft` 상태가 아니면 아무것도 바꾸지 않고 False.
    """

    def _write(conn: sqlite3.Connection) -> bool:
        conn.execute("BEGIN")
        try:
            saved = _apply_compose_fields(
                conn,
                draft_id,
                to_addrs=to_addrs,
                cc_addrs=cc_addrs,
                bcc_addrs=bcc_addrs,
                subject=subject,
                body_text=body_text,
                attachments=attachments,
                recipient_norms=recipient_norms,
            )
            if not saved:
                conn.rollback()
                return False
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE drafts SET message_id_hdr = ?, "
                "outbox_expires_at = NULL, approved_by = ?, approved_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'draft'",
                (message_id_hdr, approved_by, now, now, draft_id),
            )
            # transition()이 커밋한다 — 위 저장과 전이가 한 번에 반영된다.
            ok = transition(conn, "drafts", draft_id, ["draft"], "outbox")
        except BaseException:
            conn.rollback()
            raise
        return ok

    return db.writer.submit(_write).result()


def list_by_status(
    conn: sqlite3.Connection, statuses: list[str], *, account_id: int | None = None
) -> list[DraftRow]:
    """작업(보낼편지함/승인 대기) 가상 폴더용 — `status`가 목록 중 하나인 초안을 가져온다."""
    placeholders = ",".join("?" for _ in statuses)
    params: list[object] = list(statuses)
    sql = f"SELECT * FROM drafts WHERE status IN ({placeholders})"
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    sql += " ORDER BY updated_at DESC"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_draft(row) for row in rows]


def count_by_status(
    conn: sqlite3.Connection, statuses: list[str], *, account_id: int | None = None
) -> int:
    placeholders = ",".join("?" for _ in statuses)
    params: list[object] = list(statuses)
    sql = f"SELECT COUNT(*) FROM drafts WHERE status IN ({placeholders})"
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    row = conn.execute(sql, params).fetchone()
    return int(row[0])


def delete_draft(db: Database, draft_id: int, *, mcp_owner_token_id: int | None = None) -> bool:
    """작성 중(`status='draft'`) 초안만 완전히 지운다(발송 이력이 있는 초안은 보존).

    `mcp_owner_token_id`를 주면(MCP `delete_draft` 경로) 그 토큰이 만든 `origin='mcp'`
    초안일 때만 지운다(`update_compose`와 같은 이유, 보안검토 N-1).
    """

    def _write(conn: sqlite3.Connection) -> bool:
        conn.execute("BEGIN")
        try:
            sql = "DELETE FROM drafts WHERE id = ? AND status = 'draft'"
            params: list[object] = [draft_id]
            if mcp_owner_token_id is not None:
                sql += " AND origin = 'mcp' AND mcp_token_id = ?"
                params.append(mcp_owner_token_id)
            cursor = conn.execute(sql, params)
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return cursor.rowcount == 1

    return db.writer.submit(_write).result()


def update_body(db: Database, draft_id: int, *, subject: str | None, body_text: str | None) -> None:
    """`status='draft'`일 때만 편집 가능하다(§5.3) — 호출자가 상태를 먼저 확인해야 한다."""

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE drafts SET subject = ?, body_text = ?, updated_at = ? "
                "WHERE id = ? AND status = 'draft'",
                (subject, body_text, datetime.now(UTC).isoformat(), draft_id),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


def set_approval_hash(db: Database, draft_id: int, approval_hash: str, expires_at: str) -> None:
    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE drafts SET approval_hash = ?, approval_requested_at = ?, "
                "approval_expires_at = ? WHERE id = ?",
                (approval_hash, datetime.now(UTC).isoformat(), expires_at, draft_id),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


def transition_status(
    db: Database,
    draft_id: int,
    from_states: list[str],
    to_state: str,
    *,
    extra_sql: str | None = None,
    extra_params: tuple = (),
) -> bool:
    """`drafts.status`를 단일 전이 함수로 바꾼다(S-02). 성공 시 True."""

    def _write(conn: sqlite3.Connection) -> bool:
        ok = transition(conn, "drafts", draft_id, from_states, to_state)
        if ok and extra_sql:
            conn.execute("BEGIN")
            try:
                conn.execute(extra_sql, extra_params)
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        return ok

    return db.writer.submit(_write).result()


def mark_outbox(
    db: Database,
    draft_id: int,
    *,
    message_id_hdr: str,
    outbox_expires_at: str | None,
    from_states: list[str] | None = None,
    approved_by: str | None = None,
) -> bool:
    """`draft`/`pending_approval` → `outbox`. Outbox 진입 시 `message_id_hdr`을 만들어
    중복 발송 판정 기준으로 쓴다(§5.3).

    `from_states`로 출발 상태를 좁힐 수 있다 — UI의 [보내기]는 `["draft"]`만 넘긴다
    (승인 대기 중인 초안은 스냅샷 해시 검증을 거치는 승인 경로로만 Outbox에 들어가야
    한다, H9). `approved_by`를 주면 승인 주체(`user_send` 등)와 시각도 함께 기록한다 —
    MCP 레이트리밋(§7.7)이 승인 주체로 MCP 경유 발송을 구분하기 때문이다.
    """

    def _write(conn: sqlite3.Connection) -> bool:
        ok = transition(
            conn, "drafts", draft_id, from_states or ["draft", "pending_approval"], "outbox"
        )
        if ok:
            conn.execute("BEGIN")
            try:
                now = datetime.now(UTC).isoformat()
                conn.execute(
                    "UPDATE drafts SET message_id_hdr = ?, outbox_expires_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (message_id_hdr, outbox_expires_at, now, draft_id),
                )
                if approved_by is not None:
                    conn.execute(
                        "UPDATE drafts SET approved_by = ?, approved_at = ? WHERE id = ?",
                        (approved_by, now, draft_id),
                    )
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        return ok

    return db.writer.submit(_write).result()


def list_outbox_ready(conn: sqlite3.Connection, *, now_iso: str) -> list[DraftRow]:
    """`status='outbox'`이고 `next_send_at`이 지났거나 없는 초안을 가져온다(발송 대상)."""
    rows = conn.execute(
        "SELECT * FROM drafts WHERE status = 'outbox' AND "
        "(next_send_at IS NULL OR next_send_at <= ?) ORDER BY id",
        (now_iso,),
    ).fetchall()
    return [_row_to_draft(row) for row in rows]


def list_sending_stale(conn: sqlite3.Connection) -> list[DraftRow]:
    """재시작 복구 대상 — 종료 직전에 `sending`이었던 초안(§5.3 `send_unknown`)."""
    rows = conn.execute("SELECT * FROM drafts WHERE status = 'sending'").fetchall()
    return [_row_to_draft(row) for row in rows]


def mark_sent(db: Database, draft_id: int, *, sent_message_id: int | None) -> bool:
    def _write(conn: sqlite3.Connection) -> bool:
        ok = transition(conn, "drafts", draft_id, ["sending"], "sent")
        if ok:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "UPDATE drafts SET sent_at = ?, sent_message_id = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        datetime.now(UTC).isoformat(),
                        sent_message_id,
                        datetime.now(UTC).isoformat(),
                        draft_id,
                    ),
                )
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        return ok

    return db.writer.submit(_write).result()


def mark_retry(db: Database, draft_id: int, *, next_send_at: str, last_error: str) -> bool:
    """`sending` → `outbox`(Transient, 재시도). `send_attempts`를 올린다."""

    def _write(conn: sqlite3.Connection) -> bool:
        ok = transition(conn, "drafts", draft_id, ["sending"], "outbox")
        if ok:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "UPDATE drafts SET send_attempts = send_attempts + 1, next_send_at = ?, "
                    "last_error = ?, updated_at = ? WHERE id = ?",
                    (next_send_at, last_error, datetime.now(UTC).isoformat(), draft_id),
                )
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        return ok

    return db.writer.submit(_write).result()


def mark_failed(
    db: Database, draft_id: int, *, last_error: str, from_states: list[str] | None = None
) -> bool:
    def _write(conn: sqlite3.Connection) -> bool:
        ok = transition(conn, "drafts", draft_id, from_states or ["sending"], "failed")
        if ok:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "UPDATE drafts SET last_error = ?, updated_at = ? WHERE id = ?",
                    (last_error, datetime.now(UTC).isoformat(), draft_id),
                )
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        return ok

    return db.writer.submit(_write).result()


def mark_send_unknown(db: Database, draft_id: int) -> bool:
    """기동 시 복구 — `sending`이던 초안을 `send_unknown`으로 둔다(자동 재발송 금지, §5.3)."""
    return transition_status(db, draft_id, ["sending"], "send_unknown")


def mark_sending(db: Database, draft_id: int) -> bool:
    """`outbox` → `sending`. SendService가 가져갈 때 호출한다."""
    return transition_status(db, draft_id, ["outbox"], "sending")


def revert_expired_outbox_to_draft(db: Database, draft_id: int) -> bool:
    """`outbox(origin=autoreply)`가 `outbox_expires_at`(1시간)을 넘기면 `draft`로 돌린다(§5.3)."""
    return transition_status(db, draft_id, ["outbox"], "draft")


# ---------------------------------------------------------------------------
# P2 — MCP 발송 승인(§6.2, H9)과 레이트리밋(§7.7) 근거
#
# 승인 관련 전이는 "추가 컬럼 갱신(BEGIN 안) → transition()(상태 전이 + commit)" 순서로
# 하나의 트랜잭션에 묶는다. `transition()`이 마지막에 commit하므로 두 UPDATE가 함께
# 반영되고, 상태 조건(`WHERE ... AND status IN (...)`)이 두 문장 모두에 걸려 있어
# 경합 시 한쪽만 성공한다. 스냅샷 해시도 writer 스레드 안에서(=다른 쓰기가 끼어들 수
# 없는 시점에) 읽은 행으로 계산한다 — 해시 계산과 상태 전이 사이의 TOCTOU를 없앤다.
# ---------------------------------------------------------------------------

DraftVerifier = Callable[[sqlite3.Connection, DraftRow], None]


def set_mcp_token_id(db: Database, draft_id: int, token_id: int) -> None:
    """MCP로 만든 초안에 발급 토큰 ID를 남긴다(감사 추적, `drafts.mcp_token_id`)."""

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute("UPDATE drafts SET mcp_token_id = ? WHERE id = ?", (token_id, draft_id))
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


def request_approval(
    db: Database,
    draft_id: int,
    *,
    compute_hash: Callable[[DraftRow], str],
    requested_at: str,
    expires_at: str,
    verify: DraftVerifier | None = None,
) -> str | None:
    """`draft` → `pending_approval`. 스냅샷 해시를 계산해 `approval_hash`에 저장한다.

    `verify(conn, draft)`를 주면 writer 스레드 안에서(전이 직전) 호출한다 — 승인 요청
    상한 같은 검사를 하고 위반이면 예외를 던진다. 그러면 아무것도 바뀌지 않는다.

    Returns:
        저장한 스냅샷 해시. 초안이 없거나 `draft` 상태가 아니면 None.
    """

    def _write(conn: sqlite3.Connection) -> str | None:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None or row["status"] != "draft":
            return None
        draft = _row_to_draft(row)
        if verify is not None:
            verify(conn, draft)
        snapshot = compute_hash(draft)
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE drafts SET approval_hash = ?, approval_requested_at = ?, "
                "approval_expires_at = ?, approved_at = NULL, approved_by = NULL, "
                "updated_at = ? WHERE id = ? AND status = 'draft'",
                (snapshot, requested_at, expires_at, requested_at, draft_id),
            )
            ok = transition(conn, "drafts", draft_id, ["draft"], "pending_approval")
        except BaseException:
            conn.rollback()
            raise
        return snapshot if ok else None

    return db.writer.submit(_write).result()


def approve_to_outbox(
    db: Database,
    draft_id: int,
    *,
    from_states: list[str],
    verify: DraftVerifier,
    approved_by: str,
    approved_at: str,
    message_id_hdr: str,
) -> bool:
    """`from_states`(`pending_approval` 또는 allowlist 정책의 `draft`) → `outbox`.

    `verify(conn, draft)`는 writer 스레드 안에서 호출된다. 스냅샷 해시 대조, 만료,
    레이트리밋 같은 검사를 하고 위반이면 예외(`PolicyError`)를 던진다 — 그러면
    아무것도 바뀌지 않는다.

    Returns:
        전이에 성공하면 True, 초안이 없거나 상태가 이미 달라졌으면 False.
    """

    def _write(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None or row["status"] not in from_states:
            return False
        verify(conn, _row_to_draft(row))
        placeholders = ",".join("?" for _ in from_states)
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE drafts SET approved_by = ?, approved_at = ?, message_id_hdr = ?, "
                "outbox_expires_at = NULL, updated_at = ? "
                f"WHERE id = ? AND status IN ({placeholders})",
                (approved_by, approved_at, message_id_hdr, approved_at, draft_id, *from_states),
            )
            ok = transition(conn, "drafts", draft_id, from_states, "outbox")
        except BaseException:
            conn.rollback()
            raise
        return ok

    return db.writer.submit(_write).result()


def revert_approval_to_draft(
    db: Database, draft_id: int, *, hand_over_to_user: bool = False
) -> bool:
    """`pending_approval` → `draft`(거부, 만료, 수정 후 발송, 무효화). 승인 필드를 비운다.

    `hand_over_to_user=True`([수정 후 발송])이면 같은 트랜잭션에서 `origin='user'`로 바꿔
    사용자에게 소유권을 넘긴다 — 이후 그 초안을 만든 MCP 토큰은 `update_draft`/
    `delete_draft`/`send_draft`로 손댈 수 없다(보안검토 N-1, H-1 보호와 연동).
    `mcp_token_id`는 감사 추적용으로 남겨 둔다.
    """
    origin_sql = ", origin = 'user'" if hand_over_to_user else ""

    def _write(conn: sqlite3.Connection) -> bool:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE drafts SET approval_hash = NULL, approval_requested_at = NULL, "
                f"approval_expires_at = NULL, updated_at = ?{origin_sql} "
                "WHERE id = ? AND status = 'pending_approval'",
                (datetime.now(UTC).isoformat(), draft_id),
            )
            ok = transition(conn, "drafts", draft_id, ["pending_approval"], "draft")
        except BaseException:
            conn.rollback()
            raise
        return ok

    return db.writer.submit(_write).result()


def count_pending_approvals(conn: sqlite3.Connection, *, mcp_token_id: int | None) -> int:
    """해당 MCP 토큰으로 만든 초안 중 승인 대기(`pending_approval`) 건수(M-1 상한 근거).

    `mcp_token_id`가 None이면 토큰 표시가 없는 초안끼리 센다(`IS ?`는 NULL도 같다고 본다).
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM drafts WHERE status = 'pending_approval' AND mcp_token_id IS ?",
        (mcp_token_id,),
    ).fetchone()
    return int(row[0])


def list_expired_approvals(conn: sqlite3.Connection, *, now_iso: str) -> list[int]:
    """`approval_expires_at`(24시간, C-04)이 지난 승인 대기 초안 ID 목록."""
    rows = conn.execute(
        "SELECT id FROM drafts WHERE status = 'pending_approval' "
        "AND approval_expires_at IS NOT NULL AND approval_expires_at < ?",
        (now_iso,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


# MCP send_draft로 Outbox에 들어간(=발송 시도로 소비된) 상태. pending_approval/draft는 아직
# 발송이 아니므로 세지 않는다.
_MCP_SEND_CONSUMED_STATUSES = ("outbox", "sending", "sent", "failed", "send_unknown")
# 승인 주체가 이 둘이면 MCP send_draft 경로다(ui 승인은 MCP 승인 요청에만 생긴다).
_MCP_SEND_APPROVERS = ("ui", "policy_allowlist")


def count_mcp_sends_since(conn: sqlite3.Connection, since_iso: str) -> int:
    """`since_iso` 이후 MCP send_draft 경로로 Outbox에 들어간 초안 수(§7.7, S-12).

    `sent`만 세면 Outbox 처리(수 초 간격)가 따라잡기 전에 연속 호출로 상한을 넘길 수
    있으므로, Outbox 진입 시각(`approved_at`)을 기준으로 진입 이후의 모든 상태를 센다.
    """
    status_ph = ",".join("?" for _ in _MCP_SEND_CONSUMED_STATUSES)
    approver_ph = ",".join("?" for _ in _MCP_SEND_APPROVERS)
    row = conn.execute(
        f"SELECT COUNT(*) FROM drafts WHERE status IN ({status_ph}) "
        f"AND approved_by IN ({approver_ph}) AND approved_at >= ?",
        (*_MCP_SEND_CONSUMED_STATUSES, *_MCP_SEND_APPROVERS, since_iso),
    ).fetchone()
    return int(row[0])


def list_recent_drafts(
    conn: sqlite3.Connection, *, limit: int = 100, exclude_statuses: tuple[str, ...] = ()
) -> list[DraftRow]:
    """MCP `list_drafts`용 — 최근 수정 순으로 초안을 가져온다."""
    sql = "SELECT * FROM drafts"
    params: list[object] = []
    if exclude_statuses:
        sql += f" WHERE status NOT IN ({','.join('?' for _ in exclude_statuses)})"
        params.extend(exclude_statuses)
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [_row_to_draft(row) for row in conn.execute(sql, params).fetchall()]
