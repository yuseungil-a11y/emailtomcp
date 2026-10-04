from __future__ import annotations

import sqlite3

import pytest

from emailtomcp.storage.rebuild_table import rebuild_table


def test_rebuild_table_preserves_data_and_applies_new_constraint(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "r.sqlite3")
    conn.isolation_level = None
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO t (status) VALUES ('a')")
    conn.execute("INSERT INTO t (status) VALUES ('b')")

    rebuild_table(
        conn,
        "t",
        "CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT CHECK (status IN ('a','b','c')))",
        ["id", "status"],
    )

    rows = conn.execute("SELECT id, status FROM t ORDER BY id").fetchall()
    assert rows == [(1, "a"), (2, "b")]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO t (status) VALUES ('z')")


def test_rebuild_table_rolls_back_on_failure(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "r2.sqlite3")
    conn.isolation_level = None
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO t (status) VALUES ('ok')")
    conn.execute("INSERT INTO t (status) VALUES ('bad')")  # 새 CHECK를 위반할 값

    with pytest.raises(sqlite3.IntegrityError):
        rebuild_table(
            conn,
            "t",
            "CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT CHECK (status IN ('ok')))",
            ["id", "status"],
        )

    # 롤백됐으므로 원래 테이블/데이터가 그대로 남아 있어야 한다.
    rows = conn.execute("SELECT status FROM t ORDER BY id").fetchall()
    assert rows == [("ok",), ("bad",)]
