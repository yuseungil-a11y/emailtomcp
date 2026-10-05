"""`messages` 테이블 레포지토리 (DESIGN.md §5.1).

의존 규칙(§4.2)상 storage는 core에만 의존한다 — 주소 정규화(`mail.addressing`),
thread_key 산출(`mail.threading`), MIME 파싱 결과 매핑은 **호출자(mail 계층,
sync_service)**가 끝내고, 이 모듈에는 이미 계산된 평범한 값만 넘긴다.

쓰기는 모두 `storage.db.Database.writer`를 거친다. 메시지 삽입과 첨부 메타 삽입은
하나의 트랜잭션으로 묶는다(메시지는 있는데 첨부가 비는 반쪽 상태를 막기 위함).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from emailtomcp.storage.repositories.attachments import AttachmentInsert, insert_attachment_row

if TYPE_CHECKING:
    from emailtomcp.storage.db import Database


@dataclass(slots=True)
class MessageInsert:
    """삽입할 메시지 한 통(값은 모두 호출자가 미리 계산해 넘긴다)."""

    account_id: int
    folder_id: int
    message_id_hdr: str | None
    in_reply_to: str | None
    references_hdr: str | None
    thread_key: str | None
    remote_uid: str | None
    from_addr: str | None
    from_name: str | None
    from_count: int
    sender_addr: str | None
    to_addrs_json: str
    cc_addrs_json: str
    bcc_addrs_json: str
    reply_to_json: str
    from_addr_norm: str | None
    from_plain: str
    to_plain: str
    cc_plain: str
    subject: str | None
    snippet: str | None
    body_text: str | None
    body_source: str | None
    content_mismatch: bool
    has_html: bool
    date_hdr: str | None
    received_at: str
    size_bytes: int | None
    is_read: bool
    is_flagged: bool
    has_attachments: bool
    eml_path: str
    parse_limited: bool = False
    priority: int | None = None
    attachments: list[AttachmentInsert] = field(default_factory=list)
    # LoopGuard 원자료 JSON(§7.9 G1, mail/loop_headers.py). None이면 자동회신 평가에서 제외된다.
    auto_headers_json: str | None = None


@dataclass(slots=True)
class MessageRow:
    id: int
    account_id: int
    folder_id: int
    original_folder_id: int | None
    message_id_hdr: str | None
    in_reply_to: str | None
    references_hdr: str | None
    thread_key: str | None
    remote_uid: str | None
    from_addr: str | None
    from_name: str | None
    to_addrs_json: str | None
    cc_addrs_json: str | None
    bcc_addrs_json: str | None
    reply_to_json: str | None
    subject: str | None
    body_text: str | None
    body_source: str | None
    content_mismatch: bool
    has_html: bool
    date_hdr: str | None
    received_at: str
    size_bytes: int | None
    is_read: bool
    is_flagged: bool
    is_answered: bool
    has_attachments: bool
    auto_reply_status: str | None
    eml_path: str
    parse_limited: bool
    priority: int | None


def _row_to_message(row: sqlite3.Row) -> MessageRow:
    return MessageRow(
        id=row["id"],
        account_id=row["account_id"],
        folder_id=row["folder_id"],
        original_folder_id=row["original_folder_id"],
        message_id_hdr=row["message_id_hdr"],
        in_reply_to=row["in_reply_to"],
        references_hdr=row["references_hdr"],
        thread_key=row["thread_key"],
        remote_uid=row["remote_uid"],
        from_addr=row["from_addr"],
        from_name=row["from_name"],
        to_addrs_json=row["to_addrs"],
        cc_addrs_json=row["cc_addrs"],
        bcc_addrs_json=row["bcc_addrs"],
        reply_to_json=row["reply_to"],
        subject=row["subject"],
        body_text=row["body_text"],
        body_source=row["body_source"],
        content_mismatch=bool(row["content_mismatch"]),
        has_html=bool(row["has_html"]),
        date_hdr=row["date_hdr"],
        received_at=row["received_at"],
        size_bytes=row["size_bytes"],
        is_read=bool(row["is_read"]),
        is_flagged=bool(row["is_flagged"]),
        is_answered=bool(row["is_answered"]),
        has_attachments=bool(row["has_attachments"]),
        auto_reply_status=row["auto_reply_status"],
        eml_path=row["eml_path"],
        parse_limited=bool(row["parse_limited"]),
        priority=row["priority"],
    )


def get_known_remote_uids(conn: sqlite3.Connection, account_id: int, folder_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT remote_uid FROM messages WHERE account_id = ? AND folder_id = ? "
        "AND remote_uid IS NOT NULL",
        (account_id, folder_id),
    ).fetchall()
    return {row["remote_uid"] for row in rows}


def has_autoreply_handled_copy(
    conn: sqlite3.Connection, account_id: int, message_id_hdr: str | None
) -> bool:
    """같은 계정에 같은 Message-ID 사본이 있고, 그 사본이 자동회신 파이프라인에서 이미 처리됐는지.

    "처리됨" = 평가를 마쳤거나(`auto_reply_status IS NOT NULL`) 과거 메일 일괄 수신(backfill)으로
    평가에서 빠진 사본. 서버 이동 실패 상태에서 로컬만 휴지통으로 옮긴 메일이 다음 동기화 때
    INBOX에서 다시 내려와도 재평가(=잡 재생성)하지 않기 위한 조회다(데카르트 33번 L-9).
    잡 유니크 인덱스(`ux_jobs_autoreply_msg`)는 로컬 messages.id 기준이라 재수신 사본(새 id)을
    막지 못한다. Message-ID가 없으면 비교할 수 없어 False.
    """
    if not message_id_hdr:
        return False
    rows = conn.execute(
        "SELECT auto_reply_status, auto_headers FROM messages "
        "WHERE account_id = ? AND message_id_hdr = ?",
        (account_id, message_id_hdr),
    ).fetchall()
    for row in rows:
        if row["auto_reply_status"] is not None:
            return True
        try:
            headers = json.loads(row["auto_headers"]) if row["auto_headers"] else None
        except (TypeError, ValueError):
            headers = None
        if isinstance(headers, dict) and headers.get("is_backfill") is True:
            return True
    return False


def get_message(conn: sqlite3.Connection, message_id: int) -> MessageRow | None:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return _row_to_message(row) if row else None


def find_by_remote_uid(
    conn: sqlite3.Connection, account_id: int, folder_id: int, remote_uid: str
) -> MessageRow | None:
    row = conn.execute(
        "SELECT * FROM messages WHERE account_id = ? AND folder_id = ? AND remote_uid = ?",
        (account_id, folder_id, remote_uid),
    ).fetchone()
    return _row_to_message(row) if row else None


def insert_message(db: Database, data: MessageInsert) -> int:
    """메시지 + 첨부 메타를 한 트랜잭션으로 저장하고 message_id를 돌려준다."""

    def _write(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN")
        try:
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    account_id, folder_id, original_folder_id, message_id_hdr, in_reply_to,
                    references_hdr, thread_key, remote_uid, from_addr, from_name, from_count,
                    sender_addr, to_addrs, cc_addrs, bcc_addrs, reply_to, from_addr_norm,
                    from_plain, to_plain, cc_plain, subject, snippet, body_text, body_source,
                    content_mismatch, has_html, date_hdr, received_at, size_bytes, is_read,
                    is_flagged, is_answered, has_attachments, eml_path, parse_limited, priority,
                    auto_headers
                ) VALUES (
                    ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    data.account_id,
                    data.folder_id,
                    data.message_id_hdr,
                    data.in_reply_to,
                    data.references_hdr,
                    data.thread_key,
                    data.remote_uid,
                    data.from_addr,
                    data.from_name,
                    data.from_count,
                    data.sender_addr,
                    data.to_addrs_json,
                    data.cc_addrs_json,
                    data.bcc_addrs_json,
                    data.reply_to_json,
                    data.from_addr_norm,
                    data.from_plain,
                    data.to_plain,
                    data.cc_plain,
                    data.subject,
                    data.snippet,
                    data.body_text,
                    data.body_source,
                    int(data.content_mismatch),
                    int(data.has_html),
                    data.date_hdr,
                    data.received_at,
                    data.size_bytes,
                    int(data.is_read),
                    int(data.is_flagged),
                    0,
                    int(data.has_attachments),
                    data.eml_path,
                    int(data.parse_limited),
                    data.priority,
                    data.auto_headers_json,
                ),
            )
            assert cursor.lastrowid is not None
            message_id = cursor.lastrowid
            for att in data.attachments:
                insert_attachment_row(conn, message_id, att)
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
            return message_id

    return db.writer.submit(_write).result()


def set_read(db: Database, message_id: int, is_read: bool) -> None:
    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute("UPDATE messages SET is_read = ? WHERE id = ?", (int(is_read), message_id))
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


def set_flagged(db: Database, message_id: int, is_flagged: bool) -> None:
    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE messages SET is_flagged = ? WHERE id = ?", (int(is_flagged), message_id)
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


def move_to_folder(
    db: Database,
    message_id: int,
    *,
    new_folder_id: int,
    new_remote_uid: str | None,
    keep_original_folder: bool,
) -> None:
    """메시지를 다른 폴더로 옮긴다(§2(b) C-08 "삭제=휴지통 이동" 포함).

    `keep_original_folder=True`면 현재 folder_id를 `original_folder_id`에 기록해
    복구할 수 있게 한다(휴지통으로 이동할 때만 켠다).
    """

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            if keep_original_folder:
                conn.execute(
                    "UPDATE messages SET original_folder_id = folder_id, folder_id = ?, "
                    "remote_uid = COALESCE(?, remote_uid) WHERE id = ?",
                    (new_folder_id, new_remote_uid, message_id),
                )
            else:
                conn.execute(
                    "UPDATE messages SET folder_id = ?, original_folder_id = NULL, "
                    "remote_uid = COALESCE(?, remote_uid) WHERE id = ?",
                    (new_folder_id, new_remote_uid, message_id),
                )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


def update_remote_uid(db: Database, message_id: int, remote_uid: str) -> None:
    """COPYUID 없이 이동한 뒤, 다음 동기화에서 Message-ID/크기로 매칭한 결과를 반영한다."""

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE messages SET remote_uid = ? WHERE id = ?", (remote_uid, message_id)
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


def delete_message_row(db: Database, message_id: int) -> None:
    """POP3 계정의 영구 삭제(§2(b)) — 로컬 행을 지운다. `.eml` 파일 삭제는 호출자가 한다."""

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    db.writer.submit(_write).result()


def list_recent(
    conn: sqlite3.Connection, folder_id: int, *, limit: int = 200, offset: int = 0
) -> list[MessageRow]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE folder_id = ? ORDER BY received_at DESC LIMIT ? OFFSET ?",
        (folder_id, limit, offset),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


def count_unread(conn: sqlite3.Connection, folder_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE folder_id = ? AND is_read = 0", (folder_id,)
    ).fetchone()
    return int(row[0])


# 그리드 정렬에 노출하는 컬럼만 허용한다(SQL 인젝션 방지용 allowlist).
_GRID_SORT_COLUMNS: dict[str, str] = {
    "received_at": "received_at",
    "from": "from_plain",
    "subject": "subject",
    "size": "size_bytes",
    "priority": "priority",
}


def query_messages(
    conn: sqlite3.Connection,
    folder_id: int,
    *,
    limit: int,
    offset: int,
    sort_column: str = "received_at",
    descending: bool = True,
    search_text: str = "",
    unread_only: bool = False,
    flagged_only: bool = False,
    attachments_only: bool = False,
) -> tuple[list[MessageRow], int]:
    """메일 그리드용 페이지 조회(§1.2 #27 대량 성능 — fetchMore 페이징 전제).

    검색어는 3자 이상이면 FTS5(trigram) MATCH, 2자 이하이면 LIKE로 폴백한다(R7).
    """
    order_col = _GRID_SORT_COLUMNS.get(sort_column, "received_at")
    order_dir = "DESC" if descending else "ASC"

    where = ["folder_id = ?"]
    params: list[object] = [folder_id]
    if unread_only:
        where.append("is_read = 0")
    if flagged_only:
        where.append("is_flagged = 1")
    if attachments_only:
        where.append("has_attachments = 1")

    search_text = search_text.strip()
    base_table = "messages"
    if search_text:
        if len(search_text) >= 3:
            where.append("id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
            # FTS5 쿼리 문법(AND/OR/*, 괄호 등)으로 해석되지 않도록 통째로 문자열
            # 리터럴(큰따옴표로 감싼 구문)로 묶어 넘긴다 — 내부 큰따옴표는 두 번 써서 이스케이프.
            fts_term = '"' + search_text.replace('"', '""') + '"'
            params.append(fts_term)
        else:
            # `%`/`_`를 이스케이프하지 않으면 2자 이하 검색(예: "%", "_" 한 글자)이
            # 모든 메일에 일치해 버린다(BUG-②). `escape_like`+`ESCAPE '!'`로 고정한다(L4).
            where.append(
                f"(subject LIKE ? {_LIKE_ESCAPE_SQL} OR from_plain LIKE ? {_LIKE_ESCAPE_SQL} "
                f"OR body_text LIKE ? {_LIKE_ESCAPE_SQL})"
            )
            like = f"%{escape_like(search_text)}%"
            params.extend([like, like, like])

    where_sql = " AND ".join(where)
    total_row = conn.execute(
        f"SELECT COUNT(*) FROM {base_table} WHERE {where_sql}", params
    ).fetchone()
    total = int(total_row[0])

    rows = conn.execute(
        f"SELECT * FROM {base_table} WHERE {where_sql} "
        f"ORDER BY {order_col} {order_dir}, id {order_dir} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_message(row) for row in rows], total


# ---------------------------------------------------------------------------
# P2 — MCP 읽기 도구(list_messages/search_messages/get_thread)용 조회 (DESIGN.md §6.3)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MessageMeta:
    """목록·검색 결과 한 건(본문 없이 메타데이터와 snippet만)."""

    id: int
    account_id: int
    folder_id: int
    thread_key: str | None
    from_addr: str | None
    from_name: str | None
    to_addrs_json: str | None
    cc_addrs_json: str | None
    subject: str | None
    snippet: str | None
    received_at: str
    is_read: bool
    is_flagged: bool
    has_attachments: bool
    size_bytes: int | None


_META_COLUMNS = (
    "id, account_id, folder_id, thread_key, from_addr, from_name, to_addrs, cc_addrs, "
    "subject, snippet, received_at, is_read, is_flagged, has_attachments, size_bytes"
)


def _row_to_meta(row: sqlite3.Row) -> MessageMeta:
    return MessageMeta(
        id=row["id"],
        account_id=row["account_id"],
        folder_id=row["folder_id"],
        thread_key=row["thread_key"],
        from_addr=row["from_addr"],
        from_name=row["from_name"],
        to_addrs_json=row["to_addrs"],
        cc_addrs_json=row["cc_addrs"],
        subject=row["subject"],
        snippet=row["snippet"],
        received_at=row["received_at"],
        is_read=bool(row["is_read"]),
        is_flagged=bool(row["is_flagged"]),
        has_attachments=bool(row["has_attachments"]),
        size_bytes=row["size_bytes"],
    )


# LIKE 이스케이프 문자. 역슬래시는 SQL·파이썬 양쪽 따옴표 규칙이 겹쳐 실수하기 쉬워 `!`를 쓴다.
LIKE_ESCAPE_CHAR = "!"
_LIKE_ESCAPE_SQL = f"ESCAPE '{LIKE_ESCAPE_CHAR}'"


def escape_like(text: str) -> str:
    """LIKE 패턴의 `%`/`_`/`!`를 이스케이프한다(L4). `ESCAPE '!'`와 함께 쓴다."""
    esc = LIKE_ESCAPE_CHAR
    return text.replace(esc, esc + esc).replace("%", esc + "%").replace("_", esc + "_")


def _fts_phrase(term: str) -> str:
    """FTS5 MATCH 구문으로 해석되지 않도록 큰따옴표 구문으로 감싼다(L4)."""
    return '"' + term.replace('"', '""') + '"'


def list_message_meta(
    conn: sqlite3.Connection,
    *,
    folder_id: int | None = None,
    account_id: int | None = None,
    unread_only: bool = False,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MessageMeta]:
    where: list[str] = []
    params: list[object] = []
    if folder_id is not None:
        where.append("folder_id = ?")
        params.append(folder_id)
    if account_id is not None:
        where.append("account_id = ?")
        params.append(account_id)
    if unread_only:
        where.append("is_read = 0")
    if since:
        where.append("received_at >= ?")
        params.append(since)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT {_META_COLUMNS} FROM messages{where_sql} "
        "ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_meta(row) for row in rows]


def search_message_meta(
    conn: sqlite3.Connection,
    *,
    query: str,
    account_id: int | None = None,
    folder_id: int | None = None,
    from_text: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> list[MessageMeta]:
    """검색어를 공백으로 나눠 3자 이상은 FTS5(trigram), 2자 이하는 LIKE로 찾는다(R7).

    모든 단어가 일치해야 한다(AND). MATCH와 LIKE 모두 이스케이프한다(L4).
    """
    where: list[str] = []
    params: list[object] = []
    for term in query.split():
        if len(term) >= 3:
            where.append("id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
            params.append(_fts_phrase(term))
        else:
            like = f"%{escape_like(term)}%"
            where.append(
                f"(subject LIKE ? {_LIKE_ESCAPE_SQL} OR from_plain LIKE ? {_LIKE_ESCAPE_SQL} "
                f"OR body_text LIKE ? {_LIKE_ESCAPE_SQL})"
            )
            params.extend([like, like, like])
    if account_id is not None:
        where.append("account_id = ?")
        params.append(account_id)
    if folder_id is not None:
        where.append("folder_id = ?")
        params.append(folder_id)
    if from_text:
        where.append(f"from_plain LIKE ? {_LIKE_ESCAPE_SQL}")
        params.append(f"%{escape_like(from_text)}%")
    if date_from:
        where.append("received_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("received_at <= ?")
        params.append(date_to)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT {_META_COLUMNS} FROM messages{where_sql} "
        "ORDER BY received_at DESC, id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_row_to_meta(row) for row in rows]


def list_thread_messages(
    conn: sqlite3.Connection, account_id: int, thread_key: str, *, limit: int
) -> list[MessageRow]:
    """같은 계정·같은 thread_key의 최근 메시지 `limit`건(오래된 순으로 돌려준다)."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE account_id = ? AND thread_key = ? "
        "ORDER BY received_at DESC, id DESC LIMIT ?",
        (account_id, thread_key, limit),
    ).fetchall()
    return [_row_to_message(row) for row in reversed(rows)]


# ---------------------------------------------------------------------------
# P3 — 자동회신 평가 입력(DESIGN.md §7.2). 판정은 rules 패키지가 한다.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutoReplyFacts:
    """RuleEngine·드라이런이 메일 한 통을 평가할 때 쓰는 원자료(값만, 판정 없음)."""

    id: int
    account_id: int
    folder_id: int
    folder_role: str | None
    from_addr: str | None
    from_name: str | None
    from_addr_norm: str | None
    from_count: int
    sender_addr: str | None
    to_addrs_json: str | None
    cc_addrs_json: str | None
    reply_to_json: str | None
    subject: str | None
    body_text: str | None
    has_attachments: bool
    content_mismatch: bool
    date_hdr: str | None
    received_at: str
    thread_key: str | None
    auto_headers_json: str | None
    auto_reply_status: str | None
    eml_path: str


_FACTS_SQL = (
    "SELECT m.id, m.account_id, m.folder_id, f.role AS folder_role, m.from_addr, m.from_name, "
    "m.from_addr_norm, m.from_count, m.sender_addr, m.to_addrs, m.cc_addrs, m.reply_to, "
    "m.subject, m.body_text, m.has_attachments, m.content_mismatch, m.date_hdr, m.received_at, "
    "m.thread_key, m.auto_headers, m.auto_reply_status, m.eml_path "
    "FROM messages m LEFT JOIN folders f ON f.id = m.folder_id"
)


def _row_to_facts(row: sqlite3.Row) -> AutoReplyFacts:
    return AutoReplyFacts(
        id=row["id"],
        account_id=row["account_id"],
        folder_id=row["folder_id"],
        folder_role=row["folder_role"],
        from_addr=row["from_addr"],
        from_name=row["from_name"],
        from_addr_norm=row["from_addr_norm"],
        from_count=int(row["from_count"] or 0),
        sender_addr=row["sender_addr"],
        to_addrs_json=row["to_addrs"],
        cc_addrs_json=row["cc_addrs"],
        reply_to_json=row["reply_to"],
        subject=row["subject"],
        body_text=row["body_text"],
        has_attachments=bool(row["has_attachments"]),
        content_mismatch=bool(row["content_mismatch"]),
        date_hdr=row["date_hdr"],
        received_at=row["received_at"],
        thread_key=row["thread_key"],
        auto_headers_json=row["auto_headers"],
        auto_reply_status=row["auto_reply_status"],
        eml_path=row["eml_path"],
    )


def get_autoreply_facts(conn: sqlite3.Connection, message_id: int) -> AutoReplyFacts | None:
    row = conn.execute(f"{_FACTS_SQL} WHERE m.id = ?", (message_id,)).fetchone()
    return _row_to_facts(row) if row else None


def list_autoreply_catchup(
    conn: sqlite3.Connection, *, after_id: int, limit: int = 500
) -> list[AutoReplyFacts]:
    """켜기 따라잡기(§7.0 켜기 3단계): `id > watermark`이고 아직 평가 전(NULL)인 INBOX 메일."""
    rows = conn.execute(
        f"{_FACTS_SQL} WHERE m.id > ? AND m.auto_reply_status IS NULL AND f.role = 'inbox' "
        "ORDER BY m.id LIMIT ?",
        (after_id, limit),
    ).fetchall()
    return [_row_to_facts(row) for row in rows]


def list_recent_inbox_facts(
    conn: sqlite3.Connection, *, account_id: int | None = None, limit: int = 50
) -> list[AutoReplyFacts]:
    """드라이런(§11.4)용 최근 INBOX 메일."""
    where = "f.role = 'inbox'"
    params: list[object] = []
    if account_id is not None:
        where += " AND m.account_id = ?"
        params.append(account_id)
    rows = conn.execute(
        f"{_FACTS_SQL} WHERE {where} ORDER BY m.received_at DESC, m.id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_row_to_facts(row) for row in rows]
