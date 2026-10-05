"""`attachments` 테이블 레포지토리 (DESIGN.md §5.1, §8.5).

바이트 본문은 DB에 저장하지 않는다(원문 `.eml`에만 있다) — 여기서는 메타데이터만
다룬다. 의존 규칙(§4.2)상 storage는 core에만 의존한다 — 파일명 정화/위험 확장자
판정(`mail.attachments_safety`)이나 sha256 계산은 **호출자(mail 계층, sync_service)**가
미리 끝내고, 이 모듈에는 계산된 값만 평범한 타입으로 넘긴다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(slots=True)
class AttachmentInsert:
    """삽입할 첨부 메타 하나(값은 모두 호출자가 미리 계산해 넘긴다)."""

    part_index: str
    filename_raw: str | None
    filename_safe: str | None
    content_type: str | None
    size_bytes: int | None
    sha256: str | None
    content_id: str | None
    is_inline: bool
    is_dangerous: bool


@dataclass(slots=True)
class AttachmentMeta:
    id: int
    message_id: int
    part_index: str
    filename_raw: str | None
    filename_safe: str | None
    content_type: str | None
    size_bytes: int | None
    sha256: str | None
    content_id: str | None
    is_inline: bool
    is_dangerous: bool


def _row_to_attachment(row: sqlite3.Row) -> AttachmentMeta:
    return AttachmentMeta(
        id=row["id"],
        message_id=row["message_id"],
        part_index=row["part_index"],
        filename_raw=row["filename_raw"],
        filename_safe=row["filename_safe"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        content_id=row["content_id"],
        is_inline=bool(row["is_inline"]),
        is_dangerous=bool(row["is_dangerous"]),
    )


def insert_attachment_row(conn: sqlite3.Connection, message_id: int, item: AttachmentInsert) -> int:
    """writer 트랜잭션 **안에서** 호출한다(commit/rollback은 호출자가 한다).

    `messages` 레포지토리가 메시지 삽입과 같은 트랜잭션으로 호출한다.
    """
    cursor = conn.execute(
        """
        INSERT INTO attachments (
            message_id, part_index, filename_raw, filename_safe, content_type,
            detected_type, size_bytes, sha256, content_id, is_inline, is_dangerous
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            item.part_index,
            item.filename_raw,
            item.filename_safe,
            item.content_type,
            None,
            item.size_bytes,
            item.sha256,
            item.content_id,
            int(item.is_inline),
            int(item.is_dangerous),
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def list_attachments(conn: sqlite3.Connection, message_id: int) -> list[AttachmentMeta]:
    rows = conn.execute(
        "SELECT * FROM attachments WHERE message_id = ? ORDER BY id", (message_id,)
    ).fetchall()
    return [_row_to_attachment(row) for row in rows]
