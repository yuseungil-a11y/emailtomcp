"""P3 Phase B 테스트 공용 헬퍼(`test_*.py`가 아니므로 수집되지 않는다).

- 메일 저장: SyncService.to_message_insert + G1 원자료(`mail.loop_headers`)를 실제 경로대로.
- 규칙 저장: rules_repo.insert_rule(검증은 rules.schema.validate_rule을 거친다).
- FakeClaude: `core.ports.ProcessRunner` 가짜. spawn 시점에 env의 잡 토큰으로 실제 J 도구
  핸들러(tools_job)를 호출해 G5까지 실제 경로로 지나가고, stream-json을 흉내 낸다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from emailtomcp.autoreply import cli_locator
from emailtomcp.autoreply.env_policy import JOB_TOKEN_ENV, TOOL_PREFIX
from emailtomcp.autoreply.job_runner import JobRunner
from emailtomcp.config.settings import set_setting
from emailtomcp.core.clock import SystemClock
from emailtomcp.core.events import EventBus
from emailtomcp.mail.addressing import normalize_addr
from emailtomcp.mail.loop_headers import dumps_loop_headers, extract_loop_headers
from emailtomcp.mail.mime.parse import parse_eml
from emailtomcp.mail.sync_service import SyncService
from emailtomcp.mcp_server import tools_job
from emailtomcp.mcp_server.job_tokens import JobTokenStore
from emailtomcp.mcp_server.tool_base import ToolContext
from emailtomcp.rules import output_guard
from emailtomcp.rules.autosend_verify import AutoSendVerifierImpl
from emailtomcp.rules.engine import RuleEngine
from emailtomcp.rules.schema import validate_rule
from emailtomcp.storage.db import Database
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import autoreply as autoreply_repo
from emailtomcp.storage.repositories import folders as folders_repo
from emailtomcp.storage.repositories import messages as messages_repo
from emailtomcp.storage.repositories import rules as rules_repo
from emailtomcp.storage.repositories.autoreply import AutoReplyRepository

PARTNER = "partner@client.example"
ME = "me@example.com"


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / f"ar-{uuid.uuid4().hex}.sqlite3")
    db.start()
    db.migrate()
    return db


def make_account(db: Database, *, email: str = ME, auto_reply: bool = True, **kw: Any) -> int:
    fields: dict[str, Any] = {
        "display_name": "업무계정",
        "email_address": email,
        "incoming_protocol": "imap",
        "in_host": "imap.example.com",
        "in_port": 993,
        "in_security": "ssl",
        "in_username": email,
        "out_host": "smtp.example.com",
        "out_port": 465,
        "out_security": "ssl",
        "auto_reply_enabled": auto_reply,
    }
    fields.update(kw)
    return accounts_repo.insert_account(db, **fields)


def eml(
    *,
    frm: str = f"김파트너 <{PARTNER}>",
    to: str = ME,
    subject: str = "견적 문의드립니다",
    body: str = "안녕하세요. 다음 주 견적을 부탁드립니다.",
    extra_headers: str = "",
    date: datetime | None = None,
    content_type: str = "text/plain; charset=utf-8",
) -> bytes:
    stamp = format_datetime(date or datetime.now(UTC))
    head = (
        f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\nDate: {stamp}\r\n"
        f"Message-ID: <{uuid.uuid4().hex}@client.example>\r\nMIME-Version: 1.0\r\n"
        f"Content-Type: {content_type}\r\nContent-Transfer-Encoding: 8bit\r\n"
    )
    return (head + extra_headers + "\r\n" + body + "\r\n").encode("utf-8")


def store(
    db: Database,
    mail_dir: Path,
    account_id: int,
    raw: bytes,
    *,
    folder: str = "INBOX",
    role: str | None = "inbox",
    is_backfill: bool = False,
    with_headers: bool = True,
) -> int:
    mail_dir.mkdir(parents=True, exist_ok=True)
    f = folders_repo.get_or_create_folder(db, account_id, folder, role=role)
    path = mail_dir / f"{uuid.uuid4()}.eml"
    path.write_bytes(raw)
    data = SyncService.to_message_insert(
        account_id=account_id,
        folder_id=f.id,
        parsed=parse_eml(raw),
        remote_uid=None,
        eml_path=str(path),
        received_at=datetime.now(UTC),
        is_read=False,
        is_flagged=False,
        auto_headers_json=(
            dumps_loop_headers(extract_loop_headers(raw, is_backfill=is_backfill))
            if with_headers
            else None
        ),
    )
    return messages_repo.insert_message(db, data)


def add_rule(
    db: Database,
    *,
    name: str = "거래처 견적",
    action: str = "draft",
    conditions: dict | None = None,
    reply_mode: str = "generated",
    fixed_template: str | None = None,
    account_id: int | None = None,
    enabled: bool = True,
) -> int:
    """규칙을 저장한다. action=auto_send는 Phase B UI가 막으므로 검증을 건너뛰고 직접 넣는다."""
    data = {
        "name": name,
        "account_id": account_id,
        "action": "draft" if action == "auto_send" else action,
        "reply_mode": reply_mode,
        "fixed_template": fixed_template,
        "conditions": conditions
        or {
            "match": "all",
            "conditions": [{"field": "from_domain", "op": "in_list", "value": ["client.example"]}],
        },
        "max_chars": 400,
        "enabled": enabled,
    }
    validated = validate_rule(data, allow_auto_send=False)
    fields = dict(validated.fields)
    fields["action"] = action
    return rules_repo.insert_rule(db, fields=fields, enabled=enabled)


def enable(db: Database) -> int:
    conn = db.new_read_connection()
    try:
        generation = autoreply_repo.get_toggle_state(conn).generation
    finally:
        conn.close()
    assert generation is not None
    return autoreply_repo.enable(db, expected_generation=generation, now=datetime.now(UTC))


def query(db: Database, sql: str, params: tuple = ()) -> list[Any]:
    conn = db.new_read_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def make_engine(db: Database, bus: EventBus | None = None, on_job=None) -> RuleEngine:  # noqa: ANN001
    return RuleEngine(
        db,
        clock=SystemClock(),
        event_bus=bus or EventBus(),
        normalize_addr=normalize_addr,
        on_job_created=on_job,
    )


# ---------------------------------------------------------------- 가짜 claude


@dataclass
class FakeClaude:
    """ProcessRunner 가짜. 시나리오에 따라 실제 J 도구를 부르고 stream-json을 돌려준다.

    scenario: reply | skip | no_submit | extra_tool | double_submit | exit_error | timeout |
              timeout_extra_tool
    on_spawn: spawn 중(제출 뒤) 불리는 콜백(cwd) — 실행 중 설정 변경·파일 생성 흉내(M-2).
    """

    db: Database
    job_tokens: JobTokenStore
    scenario: str = "reply"
    body: str = "안녕하세요. 문의 주셔서 감사합니다. 확인 후 다시 연락드리겠습니다."
    sink: Any = None
    on_spawn: Any = None
    calls: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    _out: dict[int, bytes] = field(default_factory=dict)
    _code: dict[int, int] = field(default_factory=dict)
    _next: int = 1000

    def _ctx(self) -> ToolContext:
        return ToolContext(
            db=self.db,
            clock=SystemClock(),
            draft_service=None,  # type: ignore[arg-type]
            approval=None,  # type: ignore[arg-type]
            mail_actions=None,  # type: ignore[arg-type]
            get_setting=lambda _k, d: d,
            job_submission_sink=self.sink,
            revoke_job_token=self.job_tokens.revoke,
        )

    def spawn(self, argv, *, cwd, env, stdin_data=None):  # noqa: ANN001
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "stdin": stdin_data})
        handle = self._next
        self._next += 1
        tools: list[str] = []
        principal = self.job_tokens.authenticate(env.get(JOB_TOKEN_ENV, ""))
        ctx = self._ctx()
        if principal is not None:
            tools.append(TOOL_PREFIX + "get_job_message")
            asyncio.run(tools_job.get_job_message(ctx, principal, tools_job.NoArgs()))
            if self.scenario in ("reply", "extra_tool", "double_submit", "exit_error"):
                args = tools_job.SubmitAutoReplyArgs(decision="reply", body_text=self.body)
                asyncio.run(tools_job.submit_auto_reply(ctx, principal, args))
                tools.append(TOOL_PREFIX + "submit_auto_reply")
            elif self.scenario == "skip":
                args = tools_job.SubmitAutoReplyArgs(decision="skip", reason="사람 판단 필요")
                asyncio.run(tools_job.submit_auto_reply(ctx, principal, args))
                tools.append(TOOL_PREFIX + "submit_auto_reply")
            if self.scenario == "double_submit":
                tools.append(TOOL_PREFIX + "submit_auto_reply")
            if self.scenario in ("extra_tool", "timeout_extra_tool"):
                tools.append("Read")
        if self.on_spawn is not None:
            self.on_spawn(cwd)
        lines = [{"type": "system", "subtype": "init"}]
        for name in tools:
            lines.append(
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name}]}}
            )
        lines.append(
            {
                "type": "result",
                "subtype": "success" if self.scenario != "exit_error" else "error_during_execution",
                "is_error": self.scenario == "exit_error",
                "num_turns": len(tools) + 1,
            }
        )
        self._out[handle] = "\n".join(json.dumps(x) for x in lines).encode("utf-8")
        self._code[handle] = 1 if self.scenario == "exit_error" else 0
        self.events.append("spawn")
        return handle

    def wait(self, handle: int, *, timeout: float) -> int:
        if self.scenario.startswith("timeout"):
            raise TimeoutError("timeout")
        return self._code[handle]

    def kill_tree(self, handle: int) -> None:
        self.events.append("kill")

    def stdout_bytes(self, handle: int) -> bytes:
        return self._out.get(handle, b"")

    def release(self, handle: int) -> None:
        pass


class RecordingTokens(JobTokenStore):
    """revoke 순서를 기록하는 잡 토큰 저장소(끄기 2단계 순서 단언용)."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def revoke(self, job_id: int) -> None:
        self.events.append(f"revoke:{job_id}")
        super().revoke(job_id)


def pin_fake_cli(db: Database, tmp_path: Path) -> Path:
    exe = tmp_path / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"fake claude binary")
    set_setting(
        db,
        cli_locator.SETTING_CLI_PIN,
        {
            "program": str(exe),
            "script": None,
            "program_sha256": cli_locator.file_sha256(exe),
            "script_sha256": None,
            "pinned_at": datetime.now(UTC).isoformat(),
        },
    )
    return exe


def make_runner(
    db: Database,
    tmp_path: Path,
    *,
    fake: FakeClaude | None = None,
    tokens: JobTokenStore | None = None,
    claude_cfg: Path | None = None,
    port: int | None = 8765,
    env_source: dict[str, str] | None = None,
    managed_dirs: tuple[Path, ...] | None = None,
    config_from_env: bool = False,
) -> tuple[JobRunner, FakeClaude, JobTokenStore]:
    """config_from_env=True면 claude_config_dir를 주지 않아 env_source에서 계산하게 한다(H-1)."""
    tokens = tokens or JobTokenStore()
    fake = fake or FakeClaude(db=db, job_tokens=tokens)
    cfg = claude_cfg or (tmp_path / "claude_cfg")
    cfg.mkdir(parents=True, exist_ok=True)
    runner = JobRunner(
        db,
        clock=SystemClock(),
        repo=AutoReplyRepository(db, verifier=AutoSendVerifierImpl()),
        token_issuer=tokens,
        process_runner=fake,
        jobs_base_dir=tmp_path / "jobs",
        mcp_port=lambda: port,
        guard_check=output_guard.check,
        claude_config_dir=None if config_from_env else cfg,
        # 실제 홈 기준(운영과 같음): 홈의 ~/.claude만 조상 점검에서 제외된다. 사후 점검이 G7에서
        # 강제되므로(M-2) 가짜 홈(tmp_path)을 주면 실제 ~/.claude 때문에 모든 잡이 취소된다.
        home_dir=None,
        # 홈 키가 없으면 사전점검이 fallback3(home_missing, Spinoza 34번 N-1)이라 실제 홈을 넣는다.
        env_source=env_source
        or {
            "PATH": "",
            "SYSTEMROOT": r"C:\Windows",
            "ANTHROPIC_API_KEY": "secret",
            ("USERPROFILE" if sys.platform == "win32" else "HOME"): str(Path.home()),
        },
        managed_settings_dirs=managed_dirs if managed_dirs is not None else (),
    )
    fake.sink = runner
    return runner, fake, tokens
