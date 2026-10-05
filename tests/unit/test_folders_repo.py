"""`folders` 레포지토리 단위 테스트 (DESIGN.md §5.1, §2(b))."""

from __future__ import annotations

from pathlib import Path

import pytest

from emailtomcp.storage.repositories import folders as folders_repo
from mcp_support import make_account, make_db

pytestmark = pytest.mark.unit


def test_get_or_create_folder_creates_once_and_is_idempotent(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        first = folders_repo.get_or_create_folder(db, account_id, "INBOX", role="inbox")
        again = folders_repo.get_or_create_folder(db, account_id, "INBOX", role="inbox")
        assert first.id == again.id
        assert first.role == "inbox"

        conn = db.new_read_connection()
        try:
            assert folders_repo.get_folder(conn, first.id).id == first.id
            assert folders_repo.find_by_name(conn, account_id, "INBOX").id == first.id
            assert folders_repo.find_by_role(conn, account_id, "inbox").id == first.id
            assert folders_repo.find_by_name(conn, account_id, "없음") is None
            assert folders_repo.find_by_role(conn, account_id, "junk") is None
            assert folders_repo.get_folder(conn, 424242) is None
            names = {f.name for f in folders_repo.list_folders(conn, account_id)}
            assert names == {"INBOX"}
        finally:
            conn.close()
    finally:
        db.stop()


def test_get_or_create_folder_recovers_from_unique_constraint_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """경합(동시 두 쓰기가 같은 이름으로 생성 시도)에서도 `UNIQUE(account_id, name)` 위반을
    그대로 올리지 않고 기존 행을 조회해 돌려준다(최초 사전조회는 비었다고 가정)."""
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        existing = folders_repo.get_or_create_folder(db, account_id, "INBOX", role="inbox")

        original_find_by_name = folders_repo.find_by_name
        calls = {"n": 0}

        def _patched(conn, acc_id, name):  # noqa: ANN001, ANN202
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # 사전조회 시점엔 "아직 없다"고 속여 경합을 흉내낸다
            return original_find_by_name(conn, acc_id, name)

        monkeypatch.setattr(folders_repo, "find_by_name", _patched)
        recovered = folders_repo.get_or_create_folder(db, account_id, "INBOX", role="inbox")
        assert recovered.id == existing.id
        assert calls["n"] == 2  # 사전조회 1회 + 충돌 후 복구조회 1회
    finally:
        db.stop()


def test_update_uid_state_persists_values(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        account_id = make_account(db)
        folder = folders_repo.get_or_create_folder(db, account_id, "INBOX", role="inbox")
        folders_repo.update_uid_state(db, folder.id, uidvalidity=100, last_uid=42)

        conn = db.new_read_connection()
        try:
            updated = folders_repo.get_folder(conn, folder.id)
        finally:
            conn.close()
        assert updated.uidvalidity == 100 and updated.last_uid == 42
    finally:
        db.stop()
