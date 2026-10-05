"""`stdio_proxy`의 순수 보조 함수/CLI 진입점 가장자리 케이스 단위 테스트.

실제 stdin/stdout 중계(`run_stdio_proxy`의 메인 루프)는 실제 프로세스가 필요해
QA 보고서의 C1(실제 Claude Code 시나리오) 영역이다 — 여기서는 그 앞단(설정 오류로
조기 종료하는 경로, 로깅 설정, 기본 HTTP 클라이언트 팩토리)만 다룬다.
"""

from __future__ import annotations

import logging

import httpx2
import pytest

from emailtomcp.mcp_server.stdio_proxy import (
    HTTP_CONNECT_TIMEOUT,
    _default_http_client,
    _setup_stderr_logging,
    run_stdio_proxy,
)
from mcp_support import MemorySecretStore

pytestmark = pytest.mark.unit


def test_default_http_client_builds_configured_async_client() -> None:
    client = _default_http_client("http://127.0.0.1:1", {"Authorization": "Bearer x"})
    try:
        assert isinstance(client, httpx2.AsyncClient)
        assert client.headers["authorization"] == "Bearer x"
        assert client.timeout.connect == HTTP_CONNECT_TIMEOUT
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_setup_stderr_logging_attaches_stream_handler() -> None:
    root = logging.getLogger("emailtomcp")
    before = list(root.handlers)
    try:
        _setup_stderr_logging()
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        assert isinstance(added[0], logging.StreamHandler)
        assert root.level == logging.INFO
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)


def test_run_stdio_proxy_returns_2_on_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    """프록시 프로필 이름이 유효하지 않으면(ValueError) 안내를 찍고 2를 돌려준다
    (실제 stdio 루프에는 들어가지 않는다)."""
    exit_code = run_stdio_proxy(secret_store=MemorySecretStore(), client="bad client!")
    assert exit_code == 2
    assert "emailtomcp:" in capsys.readouterr().err
