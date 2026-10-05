"""MCP 도구 시나리오 (DESIGN.md §6.2~§6.3, §12.3 P2 시나리오 1~7, 승인 TOCTOU, 레이트리밋).

UI 승인은 `ApprovalRequested` 이벤트를 받은 "가짜 사용자"(별도 스레드)가 승인 서비스의
`approve`/`reject`를 부르는 방식으로 흉내 낸다 — UI 다이얼로그가 하는 일과 같은 경로다.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from emailtomcp.mcp_server.approval import EVENT_APPROVAL_REQUESTED, snapshot_hash_of
from emailtomcp.storage.repositories import drafts as drafts_repo
from mcp_support import McpEnv, build_env, mcp_client, store_message

pytestmark = pytest.mark.integration

ALL_SCOPES = ["read", "draft", "send", "manage"]


def _payload(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def _error_text(result: Any) -> str:
    assert result.is_error
    return result.content[0].text


def _draft(env: McpEnv, draft_id: int):  # noqa: ANN202
    conn = env.db.new_read_connection()
    try:
        return drafts_repo.get_draft(conn, draft_id)
    finally:
        conn.close()


def _fake_user(env: McpEnv, decision: str, *, delay: float = 0.05) -> list[int]:
    """승인 요청이 오면 `decision`(approve/reject)으로 응답하는 가짜 사용자."""
    seen: list[int] = []

    def _on_request(_name: str, payload: object) -> None:
        assert isinstance(payload, dict)
        draft_id = payload["draft_id"]
        seen.append(draft_id)

        def _act() -> None:
            if decision == "approve":
                draft = _draft(env, draft_id)
                env.components.approval.approve(draft_id, snapshot_hash_of(draft))
            elif decision == "reject":
                env.components.approval.reject(draft_id)

        threading.Timer(delay, _act).start()

    env.event_bus.subscribe(EVENT_APPROVAL_REQUESTED, _on_request)
    return seen


@pytest.fixture
def env(tmp_path: Path):  # noqa: ANN201
    environment = build_env(tmp_path, approval_wait_seconds=1.0)
    yield environment
    environment.close()


# ---------------------------------------------------------------- 시나리오 1~4


def test_scenario_list_accounts_search_get_message_reply(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> dict[str, Any]:
        async with mcp_client(env, token) as client:
            out: dict[str, Any] = {}
            out["accounts"] = _payload(await client.call_tool("list_accounts", {}))
            out["search3"] = _payload(
                await client.call_tool("search_messages", {"query": "견적서"})
            )
            out["search2"] = _payload(await client.call_tool("search_messages", {"query": "견적"}))
            out["search_like"] = _payload(await client.call_tool("search_messages", {"query": "%"}))
            out["message"] = _payload(
                await client.call_tool("get_message", {"message_id": env.message_id})
            )
            out["thread"] = _payload(
                await client.call_tool("get_thread", {"message_id": env.message_id})
            )
            out["reply"] = _payload(
                await client.call_tool(
                    "create_reply_draft",
                    {"message_id": env.message_id, "body_text": "확인했습니다."},
                )
            )
            return out

    out = asyncio.run(_run())
    assert out["accounts"]["accounts"][0]["email_address"] == "me@example.com"
    assert env.message_id in [m["id"] for m in out["search3"]["messages"]]
    assert env.message_id in [m["id"] for m in out["search2"]["messages"]]
    assert out["search_like"]["messages"] == []  # LIKE 와일드카드 이스케이프(L4)

    content = out["message"]["content"]
    assert content.count("<untrusted_email nonce=") == 1
    assert content.count("</untrusted_email") == 1  # 본문 속 위조 닫는 마커는 무력화
    assert "&lt;/untrusted_email>" in content
    assert "지시" in content.splitlines()[0]

    thread = out["thread"]
    assert thread["count"] == 2 and env.message_id in thread["message_ids"]
    assert "Bcc" not in thread["content"]

    reply = out["reply"]
    assert reply["status"] == "draft" and reply["to"] == ["kim@partner.example"]
    row = _draft(env, reply["draft_id"])
    assert row.origin == "mcp" and row.mcp_token_id is not None


# ---------------------------------------------------------------- 시나리오 5~7


def _create_reply(client: Any, env: McpEnv) -> Any:
    return client.call_tool(
        "create_reply_draft", {"message_id": env.message_id, "body_text": "회신 본문"}
    )


def test_scenario_send_draft_approved(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)
    seen = _fake_user(env, "approve")

    async def _run() -> dict[str, Any]:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            return _payload(await client.call_tool("send_draft", {"draft_id": draft_id}))

    result = asyncio.run(_run())
    assert result["status"] == "outbox" and result["decision"] == "approved"
    row = _draft(env, result["draft_id"])
    assert seen == [result["draft_id"]]
    assert row.status == "outbox" and row.approved_by == "ui" and row.message_id_hdr


def test_scenario_send_draft_rejected(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)
    _fake_user(env, "reject")

    async def _run() -> dict[str, Any]:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            return _payload(await client.call_tool("send_draft", {"draft_id": draft_id}))

    result = asyncio.run(_run())
    assert result["status"] == "draft" and result["decision"] == "rejected"
    row = _draft(env, result["draft_id"])
    assert row.status == "draft" and row.approval_hash is None


def test_scenario_no_response_stays_pending_then_approved_from_box(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> tuple[dict[str, Any], dict[str, Any]]:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            first = _payload(await client.call_tool("send_draft", {"draft_id": draft_id}))
            # 대기함에서 나중에 승인(UI 경로)
            outcome = env.components.approval.approve(
                draft_id, snapshot_hash_of(_draft(env, draft_id))
            )
            assert outcome.ok, outcome.message
            status = _payload(await client.call_tool("get_draft_status", {"draft_id": draft_id}))
            return first, status

    first, status = asyncio.run(_run())
    assert first["status"] == "pending_approval"
    assert status["status"] == "outbox" and status["approved_by"] == "ui"


# ---------------------------------------------------------------- 승인 TOCTOU(H9)


def test_pending_draft_cannot_be_updated_or_deleted(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> tuple[int, str, str]:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            first = _payload(await client.call_tool("send_draft", {"draft_id": draft_id}))
            assert first["status"] == "pending_approval"
            upd = await client.call_tool(
                "update_draft", {"draft_id": draft_id, "to": ["attacker@evil.example"]}
            )
            dele = await client.call_tool("delete_draft", {"draft_id": draft_id})
            return draft_id, _error_text(upd), _error_text(dele)

    draft_id, upd_err, del_err = asyncio.run(_run())
    assert "승인 대기" in upd_err and "승인 대기" in del_err
    row = _draft(env, draft_id)
    assert row.status == "pending_approval" and row.to_addrs == ["kim@partner.example"]


def test_hash_mismatch_invalidates_approval(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> int:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            assert (
                _payload(await client.call_tool("send_draft", {"draft_id": draft_id}))["status"]
                == "pending_approval"
            )
            return draft_id

    draft_id = asyncio.run(_run())
    shown_hash = snapshot_hash_of(_draft(env, draft_id))

    # 승인 대기 중에 정상 경로를 우회해 수신자를 바꿨다고 가정(직접 DB 변경).
    def _tamper(conn: Any) -> None:
        conn.execute(
            "UPDATE drafts SET to_addrs = ? WHERE id = ?",
            (json.dumps(["attacker@evil.example"]), draft_id),
        )
        conn.commit()

    env.db.writer.submit(_tamper).result()
    outcome = env.components.approval.approve(draft_id, shown_hash)
    assert not outcome.ok and outcome.decision == "invalidated"
    row = _draft(env, draft_id)
    assert row.status == "draft" and row.approval_hash is None

    # 다이얼로그가 본 해시가 다른 경우(오래된 창)도 무효.
    assert env.components.approval.request(draft_id)
    outcome2 = env.components.approval.approve(draft_id, "0" * 64)
    assert not outcome2.ok and _draft(env, draft_id).status == "draft"


def test_expired_approval_returns_to_draft(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> dict[str, Any]:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            _payload(await client.call_tool("send_draft", {"draft_id": draft_id}))

            def _expire(conn: Any) -> None:
                conn.execute(
                    "UPDATE drafts SET approval_expires_at = '2000-01-01T00:00:00+00:00' "
                    "WHERE id = ?",
                    (draft_id,),
                )
                conn.commit()

            env.db.writer.submit(_expire).result()
            return _payload(await client.call_tool("get_draft_status", {"draft_id": draft_id}))

    status = asyncio.run(_run())
    assert status["status"] == "draft"
    assert any(
        name == "ApprovalResolved" and payload.get("decision") == "expired"
        for name, payload in env.events
    )


# ---------------------------------------------------------------- 발송 정책(§6.2)


def test_allowlist_mode_sends_only_when_every_recipient_listed(env: McpEnv) -> None:
    env.setting("mcp.send_mode", "allowlist")
    env.setting("mcp.allowlist", ["partner.example"])
    token = env.token(ALL_SCOPES)

    async def _run() -> tuple[dict, dict, dict]:
        async with mcp_client(env, token) as client:
            ok_id = _payload(await _create_reply(client, env))["draft_id"]
            listed = _payload(await client.call_tool("send_draft", {"draft_id": ok_id}))
            cc_id = _payload(await _create_reply(client, env))["draft_id"]
            _payload(
                await client.call_tool(
                    "update_draft", {"draft_id": cc_id, "bcc": ["leak@outside.example"]}
                )
            )
            unlisted = _payload(await client.call_tool("send_draft", {"draft_id": cc_id}))
            fwd_id = _payload(
                await client.call_tool(
                    "create_forward_draft",
                    {"message_id": env.message_id, "to": ["kim@partner.example"], "mode": "attach"},
                )
            )["draft_id"]
            forward = _payload(await client.call_tool("send_draft", {"draft_id": fwd_id}))
            return listed, unlisted, forward

    listed, unlisted, forward = asyncio.run(_run())
    assert listed["status"] == "outbox"
    assert _draft(env, listed["draft_id"]).approved_by == "policy_allowlist"
    assert unlisted["status"] == "pending_approval"  # BCC 한 명이 목록 밖 → confirm
    assert forward["status"] == "pending_approval"  # 첨부 전달은 항상 confirm


def test_disabled_mode_and_floor_block_send(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> tuple[str, str]:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            env.setting("mcp.send_mode", "disabled")
            disabled = _error_text(await client.call_tool("send_draft", {"draft_id": draft_id}))
            env.setting("mcp.send_mode", "confirm")
            env.components.tool_server._context.send_allowed_by_floor = lambda: False
            floor = _error_text(await client.call_tool("send_draft", {"draft_id": draft_id}))
            return disabled, floor

    disabled, floor = asyncio.run(_run())
    assert "꺼져" in disabled
    assert "floor" in floor


def test_rate_limit_via_tool_20th_allowed_21st_denied(env: McpEnv) -> None:
    env.setting("mcp.send_mode", "allowlist")
    env.setting("mcp.allowlist", ["partner.example"])
    token = env.token(ALL_SCOPES)

    async def _run() -> list[Any]:
        results = []
        async with mcp_client(env, token) as client:
            for _ in range(21):
                draft_id = _payload(await _create_reply(client, env))["draft_id"]
                results.append(await client.call_tool("send_draft", {"draft_id": draft_id}))
        return results

    results = asyncio.run(_run())
    assert all(not r.is_error for r in results[:20])
    assert _payload(results[19])["status"] == "outbox"
    assert "상한" in _error_text(results[20])


# ---------------------------------------------------------------- manage 스코프


def test_manage_tools(env: McpEnv) -> None:
    token = env.token(["read", "manage"])

    async def _run() -> tuple[dict, dict, dict, dict]:
        async with mcp_client(env, token) as client:
            read = _payload(
                await client.call_tool("mark_read", {"message_ids": [env.message_id, 424242]})
            )
            flag = _payload(await client.call_tool("set_flag", {"message_ids": [env.message_id]}))
            deleted = _payload(
                await client.call_tool("delete_message", {"message_id": env.message_id})
            )
            again = _payload(
                await client.call_tool("delete_message", {"message_id": env.message_id})
            )
            return read, flag, deleted, again

    read, flag, deleted, again = asyncio.run(_run())
    assert read["updated"] == [env.message_id] and read["missing"] == [424242]
    assert flag["updated"] == [env.message_id]
    assert deleted["result"] == "moved_to_trash" and again["result"] == "already_in_trash"


def test_invalid_arguments_are_rejected(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> list[str]:
        async with mcp_client(env, token) as client:
            return [
                _error_text(
                    await client.call_tool(
                        "create_draft",
                        {"account_id": env.account_id, "to": ["not-an-address"], "subject": "x"},
                    )
                ),
                _error_text(
                    await client.call_tool(
                        "create_draft",
                        {
                            "account_id": env.account_id,
                            "to": ["a@b.example"],
                            "subject": "a\r\nBcc: x@y.z",
                        },
                    )
                ),
                _error_text(await client.call_tool("list_messages", {"limit": 51})),
                _error_text(await client.call_tool("list_accounts", {"unexpected": 1})),
            ]

    errors = asyncio.run(_run())
    assert all("오류" in e or "형식" in e or "줄바꿈" in e for e in errors)
    assert "\r\nBcc" not in errors[1]  # 입력값이 오류 메시지에 그대로 실리지 않음


# ---------------------------------------------------------------- 보안검토 P2 회귀(H-1/H-2/M-1/M-2)


def _set_attachments(env: McpEnv, draft_id: int, attachments: list[dict]) -> None:
    def _work(conn: Any) -> None:
        conn.execute(
            "UPDATE drafts SET attachments_json = ? WHERE id = ?",
            (json.dumps(attachments), draft_id),
        )
        conn.commit()

    env.db.writer.submit(_work).result()


def test_h1_mcp_tools_cannot_touch_user_drafts(env: McpEnv) -> None:
    """H-1: 사용자가 UI에서 쓰던 초안(origin=user)은 update/delete/send_draft가 거부한다."""
    env.setting("mcp.send_mode", "allowlist")
    env.setting("mcp.allowlist", ["partner.example", "outside.example"])
    user_draft = env.draft_service.create_draft(
        account_id=env.account_id,
        to_addrs=["kim@partner.example"],
        subject="사용자가 쓰던 초안",
        body_text="원래 본문",
        origin="user",
    )
    token = env.token(ALL_SCOPES)

    async def _run() -> tuple[list[dict], str, str, str]:
        async with mcp_client(env, token) as client:
            listed = _payload(await client.call_tool("list_drafts", {}))["drafts"]
            upd = await client.call_tool(
                "update_draft", {"draft_id": user_draft, "bcc": ["leak@outside.example"]}
            )
            send = await client.call_tool("send_draft", {"draft_id": user_draft})
            dele = await client.call_tool("delete_draft", {"draft_id": user_draft})
            return listed, _error_text(upd), _error_text(send), _error_text(dele)

    listed, upd_err, send_err, del_err = asyncio.run(_run())
    assert any(d["id"] == user_draft and d["origin"] == "user" for d in listed)
    for err in (upd_err, send_err, del_err):
        assert "직접 작성한 초안" in err
    row = _draft(env, user_draft)
    assert row is not None and row.status == "draft"
    assert row.bcc_addrs == [] and row.body_text == "원래 본문" and row.approved_by is None


def test_h1_other_token_cannot_touch_mcp_draft(env: McpEnv) -> None:
    """H-1(토큰 일치): 다른 MCP 토큰이 만든 초안은 수정·삭제·발송 요청할 수 없다."""
    token_a = env.token(ALL_SCOPES, label="client-a")
    token_b = env.token(ALL_SCOPES, label="client-b")

    async def _create() -> int:
        async with mcp_client(env, token_a) as client:
            return _payload(await _create_reply(client, env))["draft_id"]

    draft_id = asyncio.run(_create())

    async def _attack() -> list[str]:
        async with mcp_client(env, token_b) as client:
            return [
                _error_text(
                    await client.call_tool(
                        "update_draft", {"draft_id": draft_id, "bcc": ["leak@outside.example"]}
                    )
                ),
                _error_text(await client.call_tool("send_draft", {"draft_id": draft_id})),
                _error_text(await client.call_tool("delete_draft", {"draft_id": draft_id})),
            ]

    errors = asyncio.run(_attack())
    assert all("다른 MCP 연결" in e for e in errors)
    row = _draft(env, draft_id)
    assert row is not None and row.status == "draft" and row.bcc_addrs == []


def test_h2_allowlist_recheck_inside_writer_blocks_injected_bcc(env: McpEnv) -> None:
    """H-2(결정적 재현): 읽기 스냅샷 판정 직후, 전이 직전에 외부 BCC가 끼어들면 거부한다."""
    env.setting("mcp.send_mode", "allowlist")
    env.setting("mcp.allowlist", ["partner.example"])
    token = env.token(ALL_SCOPES)
    approval = env.components.approval
    original = approval.send_by_policy

    def _racy_send_by_policy(draft_id: int, policy_check: Any) -> bool:
        # 경합 창에서 update_draft가 먼저 끝났다고 가정한다(같은 writer 경로로 수정).
        current = _draft(env, draft_id)
        assert env.draft_service.update_compose(
            draft_id,
            to_addrs=current.to_addrs,
            cc_addrs=current.cc_addrs,
            bcc_addrs=["leak@outside.example"],
            subject=current.subject,
            body_text=current.body_text,
            attachments=current.attachments,
        )
        return original(draft_id, policy_check)

    approval.send_by_policy = _racy_send_by_policy  # type: ignore[method-assign]
    try:

        async def _run() -> tuple[int, str]:
            async with mcp_client(env, token) as client:
                draft_id = _payload(await _create_reply(client, env))["draft_id"]
                err = _error_text(await client.call_tool("send_draft", {"draft_id": draft_id}))
                return draft_id, err

        draft_id, err = asyncio.run(_run())
    finally:
        approval.send_by_policy = original  # type: ignore[method-assign]

    assert "허용 목록에 없는 수신자" in err
    row = _draft(env, draft_id)
    assert row.status == "draft" and row.approved_by is None
    assert row.bcc_addrs == ["leak@outside.example"]


def test_h2_concurrent_send_and_update_never_sends_outsider_without_approval(env: McpEnv) -> None:
    """H-2(실제 동시 호출): send_draft와 update_draft를 동시에 보내도, 승인 없이 Outbox에
    들어간 초안에는 허용 목록 밖 수신자가 절대 없다."""
    env.setting("mcp.send_mode", "allowlist")
    env.setting("mcp.allowlist", ["partner.example"])
    token = env.token(ALL_SCOPES)

    async def _run() -> list[int]:
        ids: list[int] = []
        async with mcp_client(env, token) as client:
            for _ in range(6):
                draft_id = _payload(await _create_reply(client, env))["draft_id"]
                ids.append(draft_id)
                await asyncio.gather(
                    client.call_tool("send_draft", {"draft_id": draft_id}),
                    client.call_tool(
                        "update_draft", {"draft_id": draft_id, "bcc": ["leak@outside.example"]}
                    ),
                    return_exceptions=True,
                )
        return ids

    ids = asyncio.run(_run())
    for draft_id in ids:
        row = _draft(env, draft_id)
        if row.approved_by == "policy_allowlist":
            assert row.bcc_addrs == [], f"draft {draft_id}: 승인 없이 외부 BCC 포함 발송"
        else:
            assert row.status in ("draft", "pending_approval")


def test_m2_allowlist_mode_requires_confirm_for_any_attachment(env: McpEnv) -> None:
    """M-2: allowlist 모드에서도 첨부가 있으면 종류(회신/새 초안)와 상관없이 confirm."""
    env.setting("mcp.send_mode", "allowlist")
    env.setting("mcp.allowlist", ["partner.example"])
    token = env.token(ALL_SCOPES)

    async def _run() -> tuple[dict, dict]:
        async with mcp_client(env, token) as client:
            reply_id = _payload(await _create_reply(client, env))["draft_id"]
            _set_attachments(env, reply_id, [{"filename": "원본첨부.pdf", "size_bytes": 10}])
            reply = _payload(await client.call_tool("send_draft", {"draft_id": reply_id}))
            new_id = _payload(
                await client.call_tool(
                    "create_draft",
                    {"account_id": env.account_id, "to": ["kim@partner.example"], "subject": "s"},
                )
            )["draft_id"]
            _set_attachments(env, new_id, [{"filename": "local.txt", "size_bytes": 5}])
            new = _payload(await client.call_tool("send_draft", {"draft_id": new_id}))
            return reply, new

    reply, new = asyncio.run(_run())
    assert reply["status"] == "pending_approval"
    assert new["status"] == "pending_approval"


def test_m1_pending_approval_cap_per_token(tmp_path: Path) -> None:
    """M-1: 한 토큰의 동시 승인 대기는 3건까지, 4번째 요청은 명확한 오류로 거부."""
    environment = build_env(tmp_path, approval_wait_seconds=0.05)
    try:
        token = environment.token(ALL_SCOPES)

        async def _run() -> list[Any]:
            results = []
            async with mcp_client(environment, token) as client:
                for _ in range(4):
                    draft_id = _payload(await _create_reply(client, environment))["draft_id"]
                    results.append(await client.call_tool("send_draft", {"draft_id": draft_id}))
            return results

        results = asyncio.run(_run())
    finally:
        environment.close()
    assert [_payload(r)["status"] for r in results[:3]] == ["pending_approval"] * 3
    assert "최대 3건" in _error_text(results[3])


def test_m1_rejected_draft_cannot_be_rerequested_immediately(env: McpEnv) -> None:
    """M-1: 사용자가 거부한 초안을 곧바로 다시 승인 요청하면 쿨다운으로 거부한다."""
    token = env.token(ALL_SCOPES)
    _fake_user(env, "reject")

    async def _run() -> tuple[dict, str]:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            first = _payload(await client.call_tool("send_draft", {"draft_id": draft_id}))
            again = _error_text(await client.call_tool("send_draft", {"draft_id": draft_id}))
            return first, again

    first, again = asyncio.run(_run())
    assert first["decision"] == "rejected"
    assert "방금 이 초안의 발송을 거부" in again
    assert _draft(env, first["draft_id"]).status == "draft"


# ---------------------------------------------------------------- 2차 재검증 회귀(N-1/R-1, N-4)


def _pending_then_hand_over(env: McpEnv, token: str) -> int:
    """MCP가 초안을 만들어 승인 요청 → 사용자가 [수정 후 발송](cancel_for_edit)으로 넘겨받음."""

    async def _run() -> int:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            first = _payload(await client.call_tool("send_draft", {"draft_id": draft_id}))
            assert first["status"] == "pending_approval"
            return draft_id

    draft_id = asyncio.run(_run())
    assert env.components.approval.cancel_for_edit(draft_id)
    return draft_id


def test_n1_hand_over_blocks_original_token_update_delete_send(env: McpEnv) -> None:
    """N-1: [수정 후 발송]으로 사용자가 넘겨받은 초안은 만든 토큰도 더는 손댈 수 없다."""
    token = env.token(ALL_SCOPES)
    draft_id = _pending_then_hand_over(env, token)
    row = _draft(env, draft_id)
    assert row.status == "draft" and row.origin == "user"
    assert row.mcp_token_id is not None  # 감사 추적용으로 남는다
    # 넘겨받은 뒤 allowlist가 외부 도메인까지 허용하더라도(막히지 않으면 실제 발송되는
    # 조건) 소유권 검사로 거부되어야 한다.
    env.setting("mcp.send_mode", "allowlist")
    env.setting("mcp.allowlist", ["partner.example", "outside.example"])

    async def _attack() -> list[str]:
        async with mcp_client(env, token) as client:
            return [
                _error_text(
                    await client.call_tool(
                        "update_draft", {"draft_id": draft_id, "bcc": ["leak@outside.example"]}
                    )
                ),
                _error_text(await client.call_tool("send_draft", {"draft_id": draft_id})),
                _error_text(await client.call_tool("delete_draft", {"draft_id": draft_id})),
            ]

    errors = asyncio.run(_attack())
    assert all("직접 작성한 초안" in e for e in errors)
    row = _draft(env, draft_id)
    assert row.status == "draft" and row.bcc_addrs == [] and row.approved_by is None


def test_n1_writer_side_owner_guard_blocks_stale_ownership_check(env: McpEnv) -> None:
    """N-1: 사전 소유권 확인(읽기 스냅샷)을 통과한 뒤 사용자가 넘겨받았어도, writer 안의
    소유권 조건이 MCP 쓰기를 막는다(update/delete/승인 요청)."""
    token = env.token(ALL_SCOPES)
    draft_id = _pending_then_hand_over(env, token)
    owner = _draft(env, draft_id).mcp_token_id
    assert owner is not None

    assert not env.draft_service.update_compose(
        draft_id,
        to_addrs=["kim@partner.example"],
        bcc_addrs=["leak@outside.example"],
        subject="변조",
        body_text="변조",
        mcp_owner_token_id=owner,
    )
    assert not env.draft_service.discard_draft(draft_id, mcp_owner_token_id=owner)

    from emailtomcp.core.errors import PolicyError
    from emailtomcp.mcp_server.auth import Principal
    from emailtomcp.mcp_server.tools_draft import require_mcp_owned

    principal = Principal(kind="interactive", scopes=frozenset(ALL_SCOPES), token_id=owner)
    with pytest.raises(PolicyError):
        env.components.approval.request(
            draft_id, lambda current: require_mcp_owned(current, principal, "발송 요청")
        )
    row = _draft(env, draft_id)
    assert row.status == "draft" and row.bcc_addrs == [] and row.subject != "변조"


def test_n1_compose_send_saves_and_queues_atomically(env: McpEnv) -> None:
    """N-1/R-1: 작성 창 [보내기]는 화면 내용 저장과 Outbox 전이를 한 writer 작업으로 한다.

    작성 창이 내용을 읽어 간 뒤 같은 토큰의 update_draft가(최악의 경우 소유권 확인을
    통과해) 외부 BCC를 끼워 넣었더라도, 실제로 Outbox에 들어가는 내용은 화면 내용이다.
    전이 이후에는 update_draft가 끼어들 수 없다.
    """
    token = env.token(ALL_SCOPES)

    async def _create() -> int:
        async with mcp_client(env, token) as client:
            return _payload(await _create_reply(client, env))["draft_id"]

    draft_id = asyncio.run(_create())
    shown = _draft(env, draft_id)  # 작성 창이 보여 준 내용

    async def _inject() -> dict[str, Any]:
        async with mcp_client(env, token) as client:
            return _payload(
                await client.call_tool(
                    "update_draft", {"draft_id": draft_id, "bcc": ["leak@outside.example"]}
                )
            )

    assert asyncio.run(_inject())["bcc"] == ["leak@outside.example"]

    assert env.draft_service.save_compose_and_mark_outbox(
        draft_id,
        to_addrs=shown.to_addrs,
        cc_addrs=shown.cc_addrs,
        bcc_addrs=shown.bcc_addrs,
        subject=shown.subject,
        body_text=shown.body_text,
        attachments=shown.attachments,
        message_id_hdr="<n1-test@example.com>",
        approved_by="user_send",
    )
    row = _draft(env, draft_id)
    assert row.status == "outbox" and row.approved_by == "user_send"
    assert row.bcc_addrs == [] and row.to_addrs == shown.to_addrs
    assert row.message_id_hdr == "<n1-test@example.com>"
    conn = env.db.new_read_connection()
    try:
        kinds = conn.execute(
            "SELECT kind, addr_norm FROM draft_recipients WHERE draft_id = ?", (draft_id,)
        ).fetchall()
    finally:
        conn.close()
    assert all(k["addr_norm"] != "leak@outside.example" for k in kinds)

    # 전이 이후의 update_draft는 상태 조건에 막힌다.
    late = asyncio.run(_late_update(env, token, draft_id))
    assert "draft" in late
    assert _draft(env, draft_id).bcc_addrs == []


async def _late_update(env: McpEnv, token: str, draft_id: int) -> str:
    async with mcp_client(env, token) as client:
        return _error_text(
            await client.call_tool(
                "update_draft", {"draft_id": draft_id, "bcc": ["leak@outside.example"]}
            )
        )


def test_n1_compose_send_does_nothing_when_not_draft(env: McpEnv) -> None:
    """작성 창 [보내기]: 초안이 이미 승인 대기 등 다른 상태면 내용도 상태도 바꾸지 않는다."""
    token = env.token(ALL_SCOPES)

    async def _run() -> int:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            assert (
                _payload(await client.call_tool("send_draft", {"draft_id": draft_id}))["status"]
                == "pending_approval"
            )
            return draft_id

    draft_id = asyncio.run(_run())
    assert not env.draft_service.save_compose_and_mark_outbox(
        draft_id,
        to_addrs=["someone@else.example"],
        subject="바꿔치기",
        body_text="x",
        message_id_hdr="<n1-noop@example.com>",
        approved_by="user_send",
    )
    row = _draft(env, draft_id)
    assert row.status == "pending_approval" and row.to_addrs == ["kim@partner.example"]
    assert row.message_id_hdr is None and row.approved_by is None


def test_n4_principal_without_token_id_is_rejected(env: McpEnv) -> None:
    """N-4: token_id가 없는 주체는 초안 쪽 mcp_token_id도 NULL이어도 통과하지 못한다."""
    from emailtomcp.core.errors import PolicyError
    from emailtomcp.mcp_server.auth import Principal
    from emailtomcp.mcp_server.tools_draft import require_mcp_owned

    draft_id = env.draft_service.create_draft(
        account_id=env.account_id,
        to_addrs=["kim@partner.example"],
        subject="토큰 표시 없는 MCP 초안",
        body_text="본문",
        origin="mcp",
    )
    row = _draft(env, draft_id)
    assert row.origin == "mcp" and row.mcp_token_id is None
    principal = Principal(kind="job", scopes=frozenset(ALL_SCOPES), token_id=None)
    with pytest.raises(PolicyError, match="토큰으로 식별되지 않은"):
        require_mcp_owned(row, principal, "수정")


# ---------------------------------------------------------------- 나머지 read 도구(갈릴레오 C2 커버리지 보강)

# Cc 2명·첨부 1건·plain/html 내용 불일치가 모두 있는 메일(get_message 헤더·경고 분기 보강용).
CC_ATTACH_MISMATCH_EML = (
    b"From: sender@example.com\r\n"
    b"To: me@example.com\r\n"
    b"Cc: cc1@example.com, cc2@example.com\r\n"
    b"Subject: Cc attachment mismatch test\r\n"
    b"Date: Mon, 1 Sep 2025 11:00:00 +0900\r\n"
    b"Message-ID: <q-003@example.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="OUTER"\r\n'
    b"\r\n"
    b"--OUTER\r\n"
    b'Content-Type: multipart/alternative; boundary="INNER"\r\n'
    b"\r\n"
    b"--INNER\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n"
    b"\r\n"
    b"plain alpha bravo charlie delta echo\r\n"
    b"--INNER\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n"
    b"\r\n"
    b"<p>html zulu yankee xray whiskey victor uniform tango sierra kilo lima mike "
    b"november oscar papa quebec romeo zulu yankee xray whiskey victor uniform "
    b"tango sierra kilo lima mike november oscar papa quebec romeo</p>\r\n"
    b"--INNER--\r\n"
    b"--OUTER\r\n"
    b'Content-Type: text/plain; name="attach.txt"\r\n'
    b'Content-Disposition: attachment; filename="attach.txt"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    + base64.b64encode(b"hello attachment content")
    + b"\r\n"
    b"--OUTER--\r\n"
)


def test_list_folders_and_list_messages(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> dict[str, Any]:
        async with mcp_client(env, token) as client:
            return {
                "folders": _payload(
                    await client.call_tool("list_folders", {"account_id": env.account_id})
                ),
                "bad_account": _error_text(
                    await client.call_tool("list_folders", {"account_id": 424242})
                ),
                "messages": _payload(await client.call_tool("list_messages", {"limit": 10})),
                "unread": _payload(
                    await client.call_tool(
                        "list_messages", {"account_id": env.account_id, "unread_only": True}
                    )
                ),
                "since": _payload(
                    await client.call_tool("list_messages", {"since": "2025-01-01T00:00:00"})
                ),
            }

    out = asyncio.run(_run())
    assert any(f["name"] == "INBOX" for f in out["folders"]["folders"])
    assert "계정을 찾을 수 없습니다" in out["bad_account"]
    assert env.message_id in [m["id"] for m in out["messages"]["messages"]]
    assert "notice" in out["messages"]
    assert isinstance(out["unread"]["messages"], list)
    assert isinstance(out["since"]["messages"], list)


def test_list_messages_rejects_invalid_since_format(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> str:
        async with mcp_client(env, token) as client:
            return _error_text(await client.call_tool("list_messages", {"since": "아무날"}))

    err = asyncio.run(_run())
    assert "ISO 8601" in err


def test_fetch_now_specific_account_invalid_account_and_all(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> tuple[dict, str, dict]:
        async with mcp_client(env, token) as client:
            one = _payload(
                await client.call_tool("fetch_now", {"account_id": env.account_id})
            )
            bad = _error_text(await client.call_tool("fetch_now", {"account_id": 424242}))
            every = _payload(await client.call_tool("fetch_now", {}))
            return one, bad, every

    one, bad, every = asyncio.run(_run())
    assert str(env.account_id) in one["results"]
    assert "계정을 찾을 수 없습니다" in bad
    assert every == {"results": {}}
    assert ("fetch_now", env.account_id) in env.mail_actions.calls
    assert ("poll_all", None) in env.mail_actions.calls


def test_get_draft_status_not_found(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> str:
        async with mcp_client(env, token) as client:
            return _error_text(await client.call_tool("get_draft_status", {"draft_id": 424242}))

    assert "초안을 찾을 수 없습니다" in asyncio.run(_run())


def test_get_message_reports_cc_attachment_mismatch_and_truncation(env: McpEnv) -> None:
    new_id = store_message(env.db, env.mail_dir, env.account_id, CC_ATTACH_MISMATCH_EML)
    token = env.token(ALL_SCOPES)

    async def _run() -> dict[str, Any]:
        async with mcp_client(env, token) as client:
            return _payload(
                await client.call_tool("get_message", {"message_id": new_id, "max_chars": 100})
            )

    result = asyncio.run(_run())
    assert result["truncated"] is True
    assert result["has_attachments"] is True
    assert "Cc: cc1@example.com, cc2@example.com" in result["content"]
    assert any("H5" in w for w in result["warnings"])


def test_get_message_warns_on_parse_limited_flag(env: McpEnv) -> None:
    """parse_limited는 MIME 파서가 직접 세우는 플래그다 — 여기서는 get_message가 그 값을
    경고 문구로 그대로 드러내는지만 확인한다(값 자체를 만드는 로직은 mime/parse 테스트가 다룸)."""

    def _mark_limited(conn: Any) -> None:
        conn.execute("UPDATE messages SET parse_limited = 1 WHERE id = ?", (env.message_id,))
        conn.commit()

    env.db.writer.submit(_mark_limited).result()
    token = env.token(ALL_SCOPES)

    async def _run() -> dict[str, Any]:
        async with mcp_client(env, token) as client:
            return _payload(
                await client.call_tool("get_message", {"message_id": env.message_id})
            )

    result = asyncio.run(_run())
    assert any("MIME 상한" in w for w in result["warnings"])


def test_get_message_not_found(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> str:
        async with mcp_client(env, token) as client:
            return _error_text(await client.call_tool("get_message", {"message_id": 424242}))

    assert "메일을 찾을 수 없습니다" in asyncio.run(_run())


def test_get_thread_not_found_missing_thread_key_and_long_body_truncated(env: McpEnv) -> None:
    long_body = (
        "From: solo@example.com\r\n"
        "To: me@example.com\r\n"
        "Subject: 외톨이 메일\r\n"
        "Date: Mon, 1 Sep 2025 12:00:00 +0900\r\n"
        "Message-ID: <solo-001@example.com>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n" + ("가" * 2500) + "\r\n"
    ).encode()
    solo_id = store_message(env.db, env.mail_dir, env.account_id, long_body)

    def _clear_thread_key(conn: Any) -> None:
        # 레거시 데이터 시뮬레이션: thread_key가 비어 있는 메시지(§5.5 C-09 예외 경로).
        conn.execute("UPDATE messages SET thread_key = NULL WHERE id = ?", (solo_id,))
        conn.commit()

    env.db.writer.submit(_clear_thread_key).result()
    token = env.token(ALL_SCOPES)

    async def _run() -> tuple[str, dict[str, Any]]:
        async with mcp_client(env, token) as client:
            missing = _error_text(await client.call_tool("get_thread", {"message_id": 424242}))
            solo = _payload(await client.call_tool("get_thread", {"message_id": solo_id}))
            return missing, solo

    missing, solo = asyncio.run(_run())
    assert "메일을 찾을 수 없습니다" in missing
    assert solo["count"] == 1 and solo["message_ids"] == [solo_id]
    # tools_read.py는 "…(이하 생략)"(유니코드 말줄임표)를 붙이지만, 응답 직전
    # wrap_untrusted()→sanitize_untrusted_text()의 NFKC 정규화가 "…"를 "..."(마침표 3개)로
    # 바꾼다(mail/untrusted.py). 그래서 실제 content에는 "...(이하 생략)"로 나온다.
    assert "(이하 생략)" in solo["content"]


# ---------------------------------------------------------------- draft/manage "대상 없음" 오류(갈릴레오 C2)


def test_draft_tools_report_not_found_errors(env: McpEnv) -> None:
    token = env.token(ALL_SCOPES)

    async def _run() -> dict[str, str]:
        async with mcp_client(env, token) as client:
            return {
                "update": _error_text(
                    await client.call_tool(
                        "update_draft", {"draft_id": 424242, "subject": "x"}
                    )
                ),
                "delete": _error_text(
                    await client.call_tool("delete_draft", {"draft_id": 424242})
                ),
                "reply": _error_text(
                    await client.call_tool(
                        "create_reply_draft", {"message_id": 424242, "body_text": "x"}
                    )
                ),
                "forward": _error_text(
                    await client.call_tool(
                        "create_forward_draft",
                        {"message_id": 424242, "to": ["a@b.example"]},
                    )
                ),
                "delete_message": _error_text(
                    await client.call_tool("delete_message", {"message_id": 424242})
                ),
            }

    errors = asyncio.run(_run())
    assert "초안을 찾을 수 없습니다" in errors["update"]
    assert "초안을 찾을 수 없습니다" in errors["delete"]
    assert "원본 메일을 찾을 수 없습니다" in errors["reply"]
    assert "원본 메일을 찾을 수 없습니다" in errors["forward"]
    assert "메일을 찾을 수 없습니다" in errors["delete_message"]


def test_update_draft_accepts_explicit_null_for_unchanged_fields(env: McpEnv) -> None:
    """cc/bcc를 명시적으로 null로 보내도(= "안 바꿈") 정상 처리된다(_check_addrs(None) 경로)."""
    token = env.token(ALL_SCOPES)

    async def _run() -> dict[str, Any]:
        async with mcp_client(env, token) as client:
            draft_id = _payload(await _create_reply(client, env))["draft_id"]
            return _payload(
                await client.call_tool(
                    "update_draft",
                    {"draft_id": draft_id, "cc": None, "bcc": None, "subject": "새 제목"},
                )
            )

    result = asyncio.run(_run())
    assert result["status"] == "draft"
