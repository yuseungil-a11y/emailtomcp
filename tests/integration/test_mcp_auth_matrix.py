"""MCP 인증·검증 매트릭스 (DESIGN.md §6.1 D-08, §12.3 P2 "인증·검증 매트릭스", S-17, M6).

in-process(`httpx2.ASGITransport` + 공식 `mcp` SDK `Client`)로 실제 소켓 없이 검증한다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx2
import pytest

from emailtomcp.storage.repositories import mcp as mcp_repo
from mcp_support import (
    MCP_HEADERS,
    TEST_PORT,
    asgi_http,
    build_env,
    jsonrpc_body,
    mcp_client,
)

pytestmark = pytest.mark.integration


def _audit_rows(env) -> list:  # noqa: ANN001
    conn = env.db.new_read_connection()
    try:
        return mcp_repo.list_audit(conn, limit=1000)
    finally:
        conn.close()


def _post(client: httpx2.AsyncClient, *, token: str | None, **overrides: object):  # noqa: ANN202
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    extra_headers = overrides.pop("headers", None)
    if isinstance(extra_headers, dict):
        headers.update(extra_headers)
    content = overrides.pop("content", None)
    if content is None:
        content = json.dumps(jsonrpc_body()).encode()
    return client.post("/mcp", headers=headers, content=content)


def test_l1_matrix_status_codes(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    token = env.token(["read"])

    async def _run() -> dict[str, int]:
        results: dict[str, int] = {}
        async with asgi_http(env) as client:
            results["no_token"] = (await _post(client, token=None)).status_code
            results["bad_token"] = (await _post(client, token="not-a-real-token")).status_code
            results["basic_scheme"] = (
                await _post(client, token=None, headers={"authorization": f"Basic {token}"})
            ).status_code
            results["origin_null"] = (
                await _post(client, token=token, headers={"origin": "null"})
            ).status_code
            results["origin_localhost"] = (
                await _post(
                    client, token=token, headers={"origin": f"http://127.0.0.1:{TEST_PORT}"}
                )
            ).status_code
            results["host_wrong_port"] = (
                await _post(client, token=token, headers={"host": "127.0.0.1:9999"})
            ).status_code
            results["host_evil"] = (
                await _post(client, token=token, headers={"host": f"evil.example:{TEST_PORT}"})
            ).status_code
            results["host_localhost_ok"] = (
                await _post(client, token=token, headers={"host": f"localhost:{TEST_PORT}"})
            ).status_code
            results["options"] = (
                await client.options("/mcp", headers={"authorization": f"Bearer {token}"})
            ).status_code
            results["get"] = (
                await client.get("/mcp", headers={"authorization": f"Bearer {token}"})
            ).status_code
            results["too_large"] = (
                await _post(client, token=token, content=b"{" + b" " * (1024 * 1024 + 10) + b"}")
            ).status_code
            results["form_type"] = (
                await _post(
                    client,
                    token=token,
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
            ).status_code
            results["ok"] = (await _post(client, token=token)).status_code
        return results

    try:
        results = asyncio.run(_run())
    finally:
        env.close()

    assert results["no_token"] == 401
    assert results["bad_token"] == 401
    assert results["basic_scheme"] == 401
    assert results["origin_null"] == 403
    assert results["origin_localhost"] == 403  # Origin은 값과 무관하게 거부
    assert results["host_wrong_port"] == 403
    assert results["host_evil"] == 403
    assert results["host_localhost_ok"] == 200
    assert results["options"] == 405
    assert results["get"] == 405
    assert results["too_large"] == 413
    assert results["form_type"] == 415
    assert results["ok"] == 200


def test_options_response_has_no_cors_headers(tmp_path: Path) -> None:
    env = build_env(tmp_path)

    async def _run() -> httpx2.Response:
        async with asgi_http(env) as client:
            return await client.options(
                "/mcp",
                headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
            )

    try:
        response = asyncio.run(_run())
    finally:
        env.close()
    assert response.status_code == 405
    assert not any(k.lower().startswith("access-control-") for k in response.headers)


def test_l2_sdk_transport_security_rejects_independently(tmp_path: Path) -> None:
    """L1을 거치지 않고 SDK 앱(L2)에 직접 보내도 Host/Origin 위반은 거부된다."""
    env = build_env(tmp_path)
    sdk_app = env.components.tool_server.build_sdk_app(TEST_PORT)

    async def _run() -> tuple[int, int, int]:
        async with (
            env.components.tool_server.session_manager.run(),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=sdk_app),
                base_url=f"http://127.0.0.1:{TEST_PORT}",
            ) as client,
        ):
            body = json.dumps(jsonrpc_body("ping")).encode()
            bad_host = await client.post(
                "/mcp", headers={**MCP_HEADERS, "host": "evil.example:8765"}, content=body
            )
            bad_origin = await client.post(
                "/mcp", headers={**MCP_HEADERS, "origin": "http://evil.example"}, content=body
            )
            null_origin = await client.post(
                "/mcp", headers={**MCP_HEADERS, "origin": "null"}, content=body
            )
            return bad_host.status_code, bad_origin.status_code, null_origin.status_code

    try:
        bad_host, bad_origin, null_origin = asyncio.run(_run())
    finally:
        env.close()
    assert bad_host in (403, 421)
    assert bad_origin == 403
    assert null_origin == 403


def test_list_tools_hides_and_call_tool_blocks_out_of_scope(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    read_token = env.token(["read"])
    draft_token = env.token(["read", "draft"], label="d")

    async def _run() -> tuple[set[str], set[str], object, object, object]:
        async with mcp_client(env, read_token) as client:
            read_tools = {t.name for t in (await client.list_tools()).tools}
            hidden_call = await client.call_tool("send_draft", {"draft_id": 1})
            unknown_call = await client.call_tool("get_job_message", {})
        async with mcp_client(env, draft_token) as client:
            draft_tools = {t.name for t in (await client.list_tools()).tools}
            manage_call = await client.call_tool("delete_message", {"message_id": env.message_id})
        return read_tools, draft_tools, hidden_call, unknown_call, manage_call

    try:
        read_tools, draft_tools, hidden_call, unknown_call, manage_call = asyncio.run(_run())
        rows = _audit_rows(env)
    finally:
        env.close()

    assert "get_message" in read_tools and "list_accounts" in read_tools
    assert not ({"send_draft", "create_draft", "delete_message"} & read_tools)
    assert "create_draft" in draft_tools and "send_draft" not in draft_tools
    for result in (hidden_call, unknown_call, manage_call):
        assert result.is_error  # type: ignore[attr-defined]
    denied = [r for r in rows if r.result == "denied" and r.tool]
    assert {r.tool for r in denied} == {"send_draft", "get_job_message", "delete_message"}
    assert all(r.http_status == 403 for r in denied)
    # 메일은 실제로 휴지통으로 가지 않았다.
    assert ("move_to_trash", env.message_id) not in env.mail_actions.calls


def test_revoked_token_is_rejected_immediately(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    issued = env.components.tokens.issue(kind="http", scopes=["read"], label="r")
    assert issued.plaintext

    async def _run() -> tuple[int, int]:
        async with asgi_http(env) as client:
            before = (await _post(client, token=issued.plaintext)).status_code
            env.components.tokens.revoke(issued.token_id)
            after = (await _post(client, token=issued.plaintext)).status_code
            return before, after

    try:
        before, after = asyncio.run(_run())
    finally:
        env.close()
    assert (before, after) == (200, 401)


def test_audit_count_matches_calls_including_denials(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    token = env.token(["read"])
    tool_calls = [
        ("list_accounts", {}),
        ("get_message", {"message_id": env.message_id}),
        ("get_message", {"message_id": 999_999}),  # 오류
        ("send_draft", {"draft_id": 1}),  # 스코프 거부
        ("search_messages", {"query": "견적서"}),
        ("list_messages", {"limit": 500}),  # 인자 오류
    ]

    async def _run() -> None:
        async with mcp_client(env, token) as client:
            for name, args in tool_calls:
                await client.call_tool(name, args)
        async with asgi_http(env) as client:
            await _post(client, token="wrong")  # L1 거부 1건

    try:
        asyncio.run(_run())
        rows = _audit_rows(env)
    finally:
        env.close()

    tool_rows = [r for r in rows if r.tool is not None]
    l1_rows = [r for r in rows if r.tool is None]
    assert len(tool_rows) == len(tool_calls)
    assert sorted(r.result for r in tool_rows) == sorted(
        ["ok", "ok", "error", "denied", "ok", "error"]
    )
    assert len(l1_rows) == 1 and l1_rows[0].http_status == 401
    for row in tool_rows:
        assert row.token_id is not None and row.token_hash8 and len(row.token_hash8) == 8
        assert token not in (row.args_masked_json or "")
