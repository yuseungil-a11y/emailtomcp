"""잡 토큰 ↔ 대화형 토큰 격리와 J 도구 (DESIGN.md §6.3 J, §7.9 G5, §7.10 N-4·L-D) — P3 Phase B.

필수 테스트 ④: 잡 토큰으로 대화형 도구를, 대화형 토큰으로 J 도구를 부르면 거부(감사 403)된다.
in-process(ASGITransport + 공식 SDK Client)로 L1(토큰 라우팅)~L3(principal.kind 분기)을 지난다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from emailtomcp.mcp_server.job_tokens import JOB_TOKEN_PREFIX
from emailtomcp.mcp_server.server import tools_for_principal
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import autoreply as autoreply_repo
from emailtomcp.storage.repositories import mcp as mcp_repo
from mcp_support import MCP_HEADERS, asgi_http, build_env, jsonrpc_body, mcp_client

pytestmark = pytest.mark.integration


class _Sink:
    def __init__(self) -> None:
        self.received: list[tuple[int, dict]] = []

    def accept_submission(self, job_id: int, **kw: object) -> None:
        self.received.append((job_id, dict(kw)))


def _running_job(env) -> int:  # noqa: ANN001
    db = env.db
    accounts_repo.update_account(db, env.account_id, auto_reply_enabled=True)
    gen = autoreply_repo.enable(db, expected_generation=1, now=datetime.now(UTC))
    job_id = autoreply_repo.create_autoreply_job(
        db,
        message_id=env.message_id,
        rule_id=None,
        rule_version=None,
        planned_action="draft",
        downgrade_reason=None,
        sender_addr_norm="kim@partner.example",
        sender_domain="partner.example",
        thread_key=None,
        expected_generation=gen,
        now=datetime.now(UTC),
    )
    assert job_id is not None
    claimed = autoreply_repo.claim_next_job(db, now=datetime.now(UTC))
    assert claimed is not None and claimed.id == job_id
    return job_id


def _audit(env) -> list:  # noqa: ANN001
    conn = env.db.new_read_connection()
    try:
        return mcp_repo.list_audit(conn, limit=500)
    finally:
        conn.close()


def test_job_and_interactive_tokens_cannot_cross_call(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    job_id = _running_job(env)
    job_token = env.components.job_tokens.issue(job_id)
    interactive = env.token(["read", "draft", "send", "manage"])

    async def _run():  # noqa: ANN202
        async with mcp_client(env, job_token) as client:
            job_tools = {t.name for t in (await client.list_tools()).tools}
            job_calls_read = await client.call_tool("list_accounts", {})
            job_calls_get_message = await client.call_tool(
                "get_message", {"message_id": env.message_id}
            )
        async with mcp_client(env, interactive) as client:
            i_tools = {t.name for t in (await client.list_tools()).tools}
            i_calls_job = await client.call_tool("get_job_message", {})
            i_calls_submit = await client.call_tool(
                "submit_auto_reply", {"decision": "reply", "body_text": "x"}
            )
        return (
            job_tools,
            i_tools,
            [job_calls_read, job_calls_get_message, i_calls_job, i_calls_submit],
        )

    try:
        job_tools, i_tools, denied_results = asyncio.run(_run())
        rows = _audit(env)
        conn = env.db.new_read_connection()
        job = autoreply_repo.get_job(conn, job_id)
        conn.close()
    finally:
        env.close()

    assert job_tools == {"get_job_message", "submit_auto_reply"}
    assert not ({"get_job_message", "submit_auto_reply"} & i_tools)
    assert "list_accounts" in i_tools
    assert all(r.is_error for r in denied_results)  # type: ignore[attr-defined]
    denied = [(r.principal, r.tool, r.http_status) for r in rows if r.result == "denied" and r.tool]
    assert ("job", "list_accounts", 403) in denied
    assert ("job", "get_message", 403) in denied
    assert ("interactive", "get_job_message", 403) in denied
    assert ("interactive", "submit_auto_reply", 403) in denied
    assert job is not None and job.submitted_at is None  # 대화형 토큰의 제출은 반영되지 않음


def test_job_token_reads_bound_message_and_submits_once(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    sink = _Sink()
    env.components.tool_server._context.job_submission_sink = sink  # 러너 대신 기록용 싱크
    job_id = _running_job(env)
    token = env.components.job_tokens.issue(job_id)

    async def _run():  # noqa: ANN202
        async with mcp_client(env, token) as client:
            message = await client.call_tool("get_job_message", {})
            submitted = await client.call_tool(
                "submit_auto_reply", {"decision": "reply", "body_text": "확인 후 회신드리겠습니다."}
            )
        async with asgi_http(env) as http:  # 제출 직후 토큰은 폐기 → 401
            again = await http.post(
                "/mcp",
                headers={**MCP_HEADERS, "authorization": f"Bearer {token}"},
                json=jsonrpc_body(),
            )
        return message, submitted, again.status_code

    try:
        message, submitted, again_status = asyncio.run(_run())
        conn = env.db.new_read_connection()
        job = autoreply_repo.get_job(conn, job_id)
        conn.close()
    finally:
        env.close()

    assert not message.is_error  # type: ignore[attr-defined]
    text = message.content[0].text  # type: ignore[attr-defined]
    assert "untrusted_email nonce=" in text and "견적서" in text
    assert "&lt;/untrusted_email" in text  # 본문의 마커 위조 시도는 이스케이프
    assert not submitted.is_error  # type: ignore[attr-defined]
    assert job is not None and job.submitted_at is not None
    assert job.submission_sha256 == autoreply_repo.sha256_text("확인 후 회신드리겠습니다.")
    assert sink.received and sink.received[0][0] == job_id
    assert sink.received[0][1]["body_text"] == "확인 후 회신드리겠습니다."
    assert again_status == 401


def test_late_submit_after_disable_is_rejected(tmp_path: Path) -> None:
    """R-c: 끈 뒤 늦게 도착한 제출은 토큰이 살아 있어도 G5(status≠running)에서 거부."""
    env = build_env(tmp_path)
    job_id = _running_job(env)
    token = env.components.job_tokens.issue(job_id)
    autoreply_repo.disable_and_drain(env.db, now=datetime.now(UTC))

    async def _run():  # noqa: ANN202
        async with mcp_client(env, token) as client:
            read = await client.call_tool("get_job_message", {})
            submit = await client.call_tool(
                "submit_auto_reply", {"decision": "reply", "body_text": "늦은 제출"}
            )
        return read, submit

    try:
        read, submit = asyncio.run(_run())
        conn = env.db.new_read_connection()
        job = autoreply_repo.get_job(conn, job_id)
        conn.close()
    finally:
        env.close()
    assert read.is_error and submit.is_error  # type: ignore[attr-defined]
    assert job is not None and job.status == "cancelled" and job.submitted_at is None


def test_token_stores_do_not_fall_back_to_each_other(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        interactive = env.token(["read"])
        job_token = env.components.job_tokens.issue(42)
        assert job_token.startswith(JOB_TOKEN_PREFIX) and "." not in interactive
        assert env.components.job_tokens.authenticate(interactive) is None
        assert env.components.tokens.authenticate(job_token) is None
        principal = env.components.job_tokens.authenticate(job_token)
        assert principal is not None and principal.kind == "job" and principal.job_id == 42
        env.components.job_tokens.revoke(42)
        assert env.components.job_tokens.authenticate(job_token) is None
    finally:
        env.close()


def test_principal_kind_is_closed_enum() -> None:
    from emailtomcp.mcp_server.auth import Principal

    assert tools_for_principal(None) == frozenset()
    weird = Principal(kind="admin", scopes=frozenset({"read", "job"}), job_id=1)
    assert tools_for_principal(weird) == frozenset()
    job_without_id = Principal(kind="job", scopes=frozenset({"job"}))
    assert tools_for_principal(job_without_id) == frozenset()
    job_with_read = Principal(kind="job", scopes=frozenset({"job", "read"}), job_id=1)
    assert tools_for_principal(job_with_read) == {"get_job_message", "submit_auto_reply"}
    interactive_with_job = Principal(kind="interactive", scopes=frozenset({"read", "job"}))
    assert not (
        {"get_job_message", "submit_auto_reply"} & tools_for_principal(interactive_with_job)
    )
