from __future__ import annotations

import threading

from emailtomcp.storage.db import Database

NOW = "2026-01-01T00:00:00Z"


def test_single_writer_serializes_concurrent_writes(tmp_path) -> None:
    db = Database(tmp_path / "w.sqlite3")
    db.start()
    try:
        db.migrate()

        def insert_account(i: int):
            def _write(conn):
                conn.execute("BEGIN")
                try:
                    conn.execute(
                        "INSERT INTO accounts (display_name, email_address, incoming_protocol, "
                        "in_host, in_port, in_security, in_username, out_host, out_port, "
                        "out_security, created_at, updated_at) "
                        "VALUES (?,?,'imap','h',993,'ssl','u','h',465,'ssl',?,?)",
                        (f"acc{i}", f"acc{i}@example.com", NOW, NOW),
                    )
                except BaseException:
                    conn.rollback()
                    raise
                else:
                    conn.commit()

            return db.writer.submit(_write)

        futures = [insert_account(i) for i in range(20)]
        for fut in futures:
            fut.result(timeout=5)

        read_conn = db.new_read_connection()
        try:
            count = read_conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        finally:
            read_conn.close()
        assert count == 20
    finally:
        db.stop()


def test_writer_runs_all_jobs_on_a_single_thread(tmp_path) -> None:
    db = Database(tmp_path / "w2.sqlite3")
    db.start()
    try:
        thread_ids: set[int] = set()

        def record(_conn):
            thread_ids.add(threading.get_ident())

        futures = [db.writer.submit(record) for _ in range(10)]
        for fut in futures:
            fut.result(timeout=5)

        assert len(thread_ids) == 1
    finally:
        db.stop()


def test_writer_propagates_exceptions_to_caller(tmp_path) -> None:
    db = Database(tmp_path / "w3.sqlite3")
    db.start()
    try:

        def boom(_conn):
            raise ValueError("의도된 실패")

        fut = db.writer.submit(boom)
        try:
            fut.result(timeout=5)
        except ValueError as exc:
            assert "의도된 실패" in str(exc)
        else:
            raise AssertionError("예외가 전달되어야 한다")
    finally:
        db.stop()


def test_writer_stop_is_idempotent(tmp_path) -> None:
    db = Database(tmp_path / "w4.sqlite3")
    db.start()
    db.stop()
    db.stop()  # 두 번 불러도 에러가 나면 안 된다
