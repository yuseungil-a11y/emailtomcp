"""송수신 이력 기반 주소 자동완성 조회 (DESIGN.md §1.2 #11, §11.2).

정식 주소록(P4)이 아니라, 작성 창의 가벼운 자동완성을 위한 조회만 담당한다.
받은 메일의 발신자(`messages.from_addr`)와 보낸 메일의 수신자(`drafts.to/cc/bcc`,
`status='sent'`)를 합쳐 "최근 주고받은 주소" 후보 목록을 만든다.
"""

from __future__ import annotations

import json
import sqlite3


def list_known_addresses(
    conn: sqlite3.Connection, account_id: int, *, limit: int = 50
) -> list[str]:
    """최근 주고받은 주소를 최신순으로 중복 없이 돌려준다(화면 표시용 원본 표기 그대로)."""
    seen: dict[str, None] = {}

    for row in conn.execute(
        "SELECT from_addr FROM messages WHERE account_id = ? AND from_addr IS NOT NULL "
        "ORDER BY received_at DESC LIMIT ?",
        (account_id, limit),
    ).fetchall():
        if row[0]:
            seen.setdefault(row[0], None)

    for row in conn.execute(
        "SELECT to_addrs, cc_addrs, bcc_addrs FROM drafts WHERE account_id = ? "
        "AND status = 'sent' ORDER BY sent_at DESC LIMIT ?",
        (account_id, limit),
    ).fetchall():
        for raw in (row[0], row[1], row[2]):
            if not raw:
                continue
            try:
                addrs = json.loads(raw)
            except (TypeError, ValueError):
                continue
            for addr in addrs:
                seen.setdefault(addr, None)

    return list(seen.keys())[:limit]
