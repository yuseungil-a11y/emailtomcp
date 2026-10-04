from __future__ import annotations

from emailtomcp.config import paths


def test_data_dir_override_env(tmp_path, monkeypatch) -> None:
    target = tmp_path / "data"
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(target))
    result = paths.data_dir()
    assert result == target
    assert result.exists()


def test_db_path_under_data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))
    assert paths.db_path() == tmp_path / "emailtomcp.sqlite3"


def test_log_dir_created(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))
    log_dir = paths.log_dir()
    assert log_dir.is_dir()
    assert log_dir == tmp_path / "logs"


def test_mail_blob_dir_created(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))
    blob_dir = paths.mail_blob_dir()
    assert blob_dir.is_dir()
    assert blob_dir == tmp_path / "mail"


def test_lock_file_path_under_data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path))
    assert paths.lock_file_path() == tmp_path / "emailtomcp.lock"
