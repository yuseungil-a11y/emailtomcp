"""앱 데이터 경로 계산.

경로는 `platformdirs`와 `pathlib`로만 다룬다(OS별 경로 하드코딩 금지, DESIGN.md §9.1).
`EMAILTOMCP_DATA_DIR` 환경변수로 전체를 오버라이드할 수 있다 — 테스트 격리와
`--data-dir` CLI 옵션(두 기능 모두 갈릴레오 §0 "단일 인스턴스와 포트" 항목의 요구사항)에 쓰인다.
"""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs

APP_NAME = "EmailToMCP"
APP_AUTHOR = "UTInfo"

ENV_DATA_DIR = "EMAILTOMCP_DATA_DIR"


def data_dir() -> Path:
    """앱 데이터 루트 디렉터리. 없으면 만든다."""
    override = os.environ.get(ENV_DATA_DIR)
    base = Path(override) if override else Path(platformdirs.user_data_dir(APP_NAME, APP_AUTHOR))
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    """SQLite DB 파일 경로."""
    return data_dir() / "emailtomcp.sqlite3"


def log_dir() -> Path:
    """로그 디렉터리. 없으면 만든다."""
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mail_blob_dir() -> Path:
    """메일 원문(.eml) 저장 디렉터리. 없으면 만든다."""
    path = data_dir() / "mail"
    path.mkdir(parents=True, exist_ok=True)
    return path


def lock_file_path() -> Path:
    """단일 인스턴스 보장을 위한 QLockFile 경로."""
    return data_dir() / "emailtomcp.lock"
