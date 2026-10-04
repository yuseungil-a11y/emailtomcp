from __future__ import annotations

from emailtomcp.config import settings
from emailtomcp.storage.db import Database


def test_set_and_get_setting_roundtrip(tmp_path) -> None:
    db = Database(tmp_path / "s.sqlite3")
    db.start()
    try:
        db.migrate()
        settings.set_setting(db, "poll_interval_sec", 300)

        read_conn = db.new_read_connection()
        try:
            value = settings.get_setting(read_conn, "poll_interval_sec")
        finally:
            read_conn.close()
        assert value == 300
    finally:
        db.stop()


def test_get_setting_default_when_missing(tmp_path) -> None:
    db = Database(tmp_path / "s2.sqlite3")
    db.start()
    try:
        db.migrate()
        read_conn = db.new_read_connection()
        try:
            value = settings.get_setting(read_conn, "nope", default="fallback")
        finally:
            read_conn.close()
        assert value == "fallback"
    finally:
        db.stop()


def test_set_setting_upserts_existing_key(tmp_path) -> None:
    db = Database(tmp_path / "s3.sqlite3")
    db.start()
    try:
        db.migrate()
        settings.set_setting(db, "theme", "light")
        settings.set_setting(db, "theme", "dark")

        read_conn = db.new_read_connection()
        try:
            value = settings.get_setting(read_conn, "theme")
            count = read_conn.execute("SELECT COUNT(*) FROM settings WHERE key='theme'").fetchone()[
                0
            ]
        finally:
            read_conn.close()
        assert value == "dark"
        assert count == 1
    finally:
        db.stop()


def test_set_setting_supports_complex_json_values(tmp_path) -> None:
    db = Database(tmp_path / "s4.sqlite3")
    db.start()
    try:
        db.migrate()
        payload = {"a": 1, "b": [1, 2, 3], "c": "한글 값"}
        settings.set_setting(db, "complex", payload)

        read_conn = db.new_read_connection()
        try:
            value = settings.get_setting(read_conn, "complex")
        finally:
            read_conn.close()
        assert value == payload
    finally:
        db.stop()
