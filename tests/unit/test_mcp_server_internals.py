"""`mcp_server.server` 내부 로직 직접 단위 테스트 (DESIGN.md §6.1, S-17, 갈릴레오 C2 보강).

통합 테스트(`tests/integration/test_mcp_auth_matrix.py` 등)는 실제 L1/L2를 거쳐 정상·
스코프 위반 경로만 검증한다. 여기서는 L1을 거치지 않은 설정 오류(principal 없음),
스코프 표와 실제 등록 도구가 어긋난 경우, 핸들러가 던지는 다양한 예외를 안전한 오류
응답으로 바꾸는 `_on_call_tool`의 분기를 직접 호출해 확인한다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest

from emailtomcp.core.errors import PermanentError, SecurityError, TransientError
from emailtomcp.mcp_server.auth import Principal
from emailtomcp.mcp_server.server import McpToolServer, principal_from_context
from emailtomcp.mcp_server.tool_base import ToolSpec
from emailtomcp.mcp_server.tools_read import NoArgs
from mcp_support import McpEnv, build_env

pytestmark = pytest.mark.unit


class _FakeRequest:
    def __init__(self, scope: Any) -> None:
        self.scope = scope


class _FakeCtx:
    def __init__(self, scope: Any = None, *, has_request: bool = True) -> None:
        if has_request:
            self.request = _FakeRequest(scope)


def _principal() -> Principal:
    return Principal(kind="interactive", scopes=frozenset({"read"}), token_id=1)


# ---------------------------------------------------------------- principal_from_context


def test_principal_from_context_handles_missing_or_malformed_scope() -> None:
    assert principal_from_context(_FakeCtx(has_request=False)) is None  # ctx.request 없음
    assert principal_from_context(_FakeCtx(scope=None)) is None  # scope가 dict 아님
    assert principal_from_context(_FakeCtx(scope={})) is None  # state 없음
    assert principal_from_context(_FakeCtx(scope={"state": {"principal": "not-a-principal"}})) is None


def test_principal_from_context_returns_principal_when_present() -> None:
    principal = _principal()
    ctx = _FakeCtx(scope={"state": {"principal": principal}})
    assert principal_from_context(ctx) is principal


# ---------------------------------------------------------------- 생성 시점 검증


def test_construction_rejects_tool_unmapped_in_scope_table() -> None:
    async def _handler(_tc: Any, _p: Any, _args: Any) -> dict[str, Any]:
        return {}

    bogus = ToolSpec("not_a_real_tool", "d", NoArgs, _handler)
    with pytest.raises(ValueError, match="스코프 표"):
        McpToolServer(tools=[bogus], context=Any, audit=Any)  # type: ignore[arg-type]


def test_tool_names_is_sorted_list_of_registered_tools(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        names = env.components.tool_server.tool_names
        assert names == sorted(names)
        assert "list_accounts" in names and "send_draft" in names
    finally:
        env.close()


# ---------------------------------------------------------------- _on_call_tool 분기


def _env(tmp_path: Path) -> McpEnv:
    return build_env(tmp_path)


def _call(env: McpEnv, principal: Principal | None, name: str, args: dict[str, Any]) -> Any:
    server = env.components.tool_server
    scope: dict[str, Any] = {"state": {}}
    if principal is not None:
        scope["state"]["principal"] = principal
    ctx = _FakeCtx(scope=scope)
    params = types.CallToolRequestParams(name=name, arguments=args)
    return asyncio.run(server._on_call_tool(ctx, params))  # noqa: SLF001


def test_on_call_tool_without_principal_is_denied_401(tmp_path: Path) -> None:
    env = _env(tmp_path)
    try:
        result = _call(env, None, "list_accounts", {})
        assert result.is_error
        assert "인증되지 않은" in result.content[0].text
    finally:
        env.close()


def test_on_call_tool_reports_unknown_tool_when_spec_missing(tmp_path: Path) -> None:
    """스코프 표에는 있지만(=is_tool_allowed True) 실제 핸들러 레지스트리에서 빠진 경우
    (설정 불일치)도 내부 오류 없이 명확한 메시지로 응답한다."""
    env = _env(tmp_path)
    try:
        server = env.components.tool_server
        server._specs.pop("list_accounts")  # noqa: SLF001 — 설정 불일치를 흉내낸다
        result = _call(env, _principal(), "list_accounts", {})
        assert result.is_error
        assert "알 수 없는 도구" in result.content[0].text
    finally:
        env.close()


def _patch_handler(env: McpEnv, name: str, handler: Any) -> None:
    server = env.components.tool_server
    old = server._specs[name]  # noqa: SLF001
    server._specs[name] = ToolSpec(old.name, old.description, old.args_model, handler)  # noqa: SLF001


def test_on_call_tool_converts_security_error_to_generic_message(tmp_path: Path) -> None:
    env = _env(tmp_path)
    try:

        async def _boom(_tc: Any, _p: Any, _args: Any) -> dict[str, Any]:
            raise SecurityError("내부 상세(토큰 유출 위험)")

        _patch_handler(env, "list_accounts", _boom)
        result = _call(env, _principal(), "list_accounts", {})
        assert result.is_error
        assert "보안 정책에 의해 거부" in result.content[0].text
        assert "내부 상세" not in result.content[0].text
    finally:
        env.close()


def test_on_call_tool_surfaces_transient_error_message(tmp_path: Path) -> None:
    env = _env(tmp_path)
    try:

        async def _boom(_tc: Any, _p: Any, _args: Any) -> dict[str, Any]:
            raise TransientError("IMAP 서버 응답 없음")

        _patch_handler(env, "list_accounts", _boom)
        result = _call(env, _principal(), "list_accounts", {})
        assert result.is_error
        assert "일시적인 오류" in result.content[0].text
        assert "IMAP 서버 응답 없음" in result.content[0].text
    finally:
        env.close()


def test_on_call_tool_hides_unexpected_exception_detail(tmp_path: Path) -> None:
    env = _env(tmp_path)
    try:

        async def _boom(_tc: Any, _p: Any, _args: Any) -> dict[str, Any]:
            raise RuntimeError("스택 트레이스에만 남아야 하는 비밀")

        _patch_handler(env, "list_accounts", _boom)
        result = _call(env, _principal(), "list_accounts", {})
        assert result.is_error
        assert result.content[0].text == "내부 오류가 발생했습니다."
    finally:
        env.close()


def test_on_call_tool_wraps_permanent_error_message(tmp_path: Path) -> None:
    env = _env(tmp_path)
    try:

        async def _boom(_tc: Any, _p: Any, _args: Any) -> dict[str, Any]:
            raise PermanentError("계정을 찾을 수 없습니다: 999")

        _patch_handler(env, "list_accounts", _boom)
        result = _call(env, _principal(), "list_accounts", {})
        assert result.is_error
        assert "계정을 찾을 수 없습니다" in result.content[0].text
    finally:
        env.close()
