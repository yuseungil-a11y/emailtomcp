"""MCP 도구 정의 공용 타입 (tools_read/tools_draft/tools_send가 공유).

- `ToolSpec`: 도구 이름·설명·인자 모델(pydantic, 추가 필드 금지)·핸들러 한 묶음.
  `inputSchema`는 인자 모델에서 만든다 — 스키마와 실제 검증이 어긋나지 않게 하기 위해서다.
- `ToolContext`: 핸들러가 쓰는 의존성 묶음. app.py(조립 루트)가 채운다. 메일 서버
  반영이 필요한 동작(즉시 수신, 휴지통 이동 등)은 `MailActions` 포트로 받는다 — P1에서
  UI용으로 이미 만든 경로(`app.UiApi`)를 그대로 재사용하기 위해서다(중복 구현 금지).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from emailtomcp.core.clock import Clock
    from emailtomcp.mail.draft_service import DraftService
    from emailtomcp.mcp_server.approval import ApprovalService
    from emailtomcp.mcp_server.auth import Principal
    from emailtomcp.storage.db import Database


class StrictArgs(BaseModel):
    """모든 도구 인자 모델의 베이스 — 정의되지 않은 인자는 거부한다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MailActions(Protocol):
    """메일 서버 반영이 섞인 동작(P1 구현 재사용). 운영 구현은 `app.UiApi`다."""

    async def fetch_now(self, account_id: int) -> int: ...

    async def poll_all(self) -> dict[int, int | str]: ...

    async def set_read(self, message_id: int, is_read: bool) -> None: ...

    async def set_flagged(self, message_id: int, is_flagged: bool) -> None: ...

    async def move_to_trash(self, message_id: int) -> None: ...


@dataclass(slots=True)
class ToolContext:
    db: Database
    clock: Clock
    draft_service: DraftService
    approval: ApprovalService
    mail_actions: MailActions
    # 설정 읽기(블로킹). 키는 §5.1 settings 주요 키(`mcp.send_mode`, `mcp.allowlist` 등).
    get_setting: Callable[[str, Any], Any]
    # min_version_floor 미달 여부(§6.1, §14.4). False면 send_draft를 거부한다.
    send_allowed_by_floor: Callable[[], bool] = field(default=lambda: True)
    approval_wait_seconds: float = 120.0
    # P3 Phase B — J 도구(tools_job.py). 제출 본문을 메모리로 넘길 곳(core.ports.JobSubmissionSink,
    # 운영 구현은 autoreply.job_runner.JobRunner — app.py가 주입)과 제출 직후 잡 토큰 폐기 함수.
    job_submission_sink: Any = None
    revoke_job_token: Callable[[int], None] | None = None


Handler = Callable[[ToolContext, "Principal", Any], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[StrictArgs]
    handler: Handler

    def input_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema(by_alias=True)
        schema.pop("title", None)
        return schema


async def run_blocking(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """블로킹 함수(DB 등)를 백엔드의 제한된 executor에서 실행한다(기본 to_thread 금지, S-01)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
