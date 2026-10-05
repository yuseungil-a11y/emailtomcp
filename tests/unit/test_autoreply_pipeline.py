"""잡 러너 파이프라인 G4 → 실행(가짜 claude) → G5 → G6 → G7 — P3 Phase B.

FakeClaude는 spawn 시 env의 잡 토큰으로 실제 J 도구(get_job_message/submit_auto_reply)를 호출해
G5(조건부 UPDATE)와 메모리 제출 경로를 그대로 지나간다.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _autoreply_helpers import (
    RecordingTokens,
    add_rule,
    eml,
    enable,
    make_account,
    make_db,
    make_engine,
    make_runner,
    pin_fake_cli,
    query,
    store,
)

from emailtomcp.autoreply import preflight
from emailtomcp.autoreply.controller import AutoReplyController
from emailtomcp.autoreply.env_policy import JOB_TOKEN_ENV
from emailtomcp.autoreply.prompts import SYSTEM_PROMPT
from emailtomcp.core.clock import SystemClock
from emailtomcp.core.events import EventBus
from emailtomcp.core.models import ApprovedBy

pytestmark = pytest.mark.unit
AUTO = ApprovedBy.POLICY_AUTO_SEND.value


@pytest.fixture
def env(tmp_path: Path):  # noqa: ANN201
    db = make_db(tmp_path)
    account = make_account(db)
    yield db, account, tmp_path
    db.stop()


def _queue_job(db, account, tmp, **rule_kw) -> int:  # noqa: ANN001
    add_rule(db, **rule_kw)
    gen = enable(db)
    engine = make_engine(db)
    engine.start(gen)
    mid = store(db, tmp / "mail", account, eml(subject="견적 문의드립니다"))
    assert engine.evaluate_message(mid, expected_generation=gen) == "queued"
    return mid


def _job_row(db):  # noqa: ANN001, ANN202
    return query(db, "SELECT * FROM auto_reply_jobs ORDER BY id DESC LIMIT 1")[0]


def test_generated_reply_creates_draft_only(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    mid = _queue_job(db, account, tmp, name="견적규칙")
    runner, fake, tokens = make_runner(db, tmp)
    assert runner.run_once() is True
    job = _job_row(db)
    assert (job["status"], job["outcome"]) == ("done", "drafted")
    draft = query(db, "SELECT * FROM drafts WHERE id = ?", (job["result_draft_id"],))[0]
    assert draft["status"] == "draft" and draft["approved_by"] is None
    assert draft["body_text"] == fake.body and draft["subject"] == "Re: 견적 문의드립니다"
    assert json.loads(draft["to_addrs"]) == ["partner@client.example"]
    assert query(db, "SELECT COUNT(*) FROM drafts WHERE approved_by = ?", (AUTO,))[0][0] == 0
    assert query(db, "SELECT auto_reply_status FROM messages WHERE id=?", (mid,))[0][0] == "drafted"
    # argv에는 상수와 앱 경로만(C-1), 사용자 문자열(규칙명)은 stdin으로(C-2)
    call = fake.calls[0]
    argv = call["argv"]
    assert "견적규칙" not in " ".join(argv) and "견적규칙" in call["stdin"].decode("utf-8")
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--append-system-prompt") + 1] == SYSTEM_PROMPT
    assert "--strict-mcp-config" in argv and "--bare" not in argv
    # env allowlist: ANTHROPIC_* 제거, 토큰은 env로만(명령줄 금지)
    assert "ANTHROPIC_API_KEY" not in call["env"]
    token = call["env"][JOB_TOKEN_ENV]
    assert token not in " ".join(argv)
    # 토큰은 끝나면 폐기, 잡 디렉터리는 삭제
    assert tokens.authenticate(token) is None
    assert not any((tmp / "jobs").iterdir())
    # 로그: 도구 목록·본문 해시만(본문 평문 없음)
    log = query(db, "SELECT * FROM auto_reply_log WHERE outcome = 'drafted'")[0]
    assert fake.body not in (log["validation_json"] or "")
    assert log["body_sha256"] == job["submission_sha256"]


def test_auto_send_rule_still_ends_as_draft(env) -> None:  # noqa: ANN001
    """필수 테스트 ①(파이프라인 전체): 규칙이 auto_send여도 결과는 draft."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp, action="auto_send")
    runner, _fake, _tokens = make_runner(db, tmp)
    runner.run_once()
    job = _job_row(db)
    assert job["downgrade_reason"] == "autosend_not_yet_supported"
    draft = query(db, "SELECT status, approved_by FROM drafts")[0]
    assert tuple(draft) == ("draft", None)
    log = query(db, "SELECT reason_code FROM auto_reply_log WHERE outcome='drafted'")[0]
    assert log[0] == "autosend_not_yet_supported"


def test_fixed_template_runs_without_claude(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    _queue_job(
        db,
        account,
        tmp,
        reply_mode="fixed_template",
        fixed_template="{sender_name_safe}님, {account_name}입니다. 확인 후 연락드리겠습니다.",
    )
    runner, fake, _tokens = make_runner(db, tmp)
    runner.run_once()
    assert fake.calls == []  # claude 미실행(CLI 미고정이어도 동작)
    draft = query(db, "SELECT body_text, status FROM drafts")[0]
    assert draft["status"] == "draft"
    assert draft["body_text"] == "김파트너님, 업무계정입니다. 확인 후 연락드리겠습니다."


def test_isolation_fallback3_cancels_job_without_running_claude(env) -> None:  # noqa: ANN001
    """필수 테스트 ⑤: 사용자 hooks/플러그인이 노출되면 draft도 만들지 않고 잡을 cancelled로."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    mid = _queue_job(db, account, tmp)
    cfg = tmp / "claude_cfg_hooks"
    cfg.mkdir()
    (cfg / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": []}]}}), encoding="utf-8"
    )
    runner, fake, tokens = make_runner(db, tmp, claude_cfg=cfg)
    runner.run_once()
    job = _job_row(db)
    assert (job["status"], job["error"]) == ("cancelled", "isolation_fallback3")
    assert fake.calls == [] and tokens.active_job_ids() == frozenset()
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0
    assert (
        query(db, "SELECT auto_reply_status FROM messages WHERE id=?", (mid,))[0][0] == "cancelled"
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"enabledPlugins": {"x@market": True}},
        {"permissions": {"allow": ["Bash(*)"]}},
        {"permissions": {"defaultMode": "bypassPermissions"}},
        {"apiKeyHelper": "/bin/echo key"},
        {"env": {"NODE_OPTIONS": "--require evil.js"}},
        "not-json{",
        # Spinoza 30번 M-1: allowlist 전환으로 새로 잡히는 항목
        {"env": {"HTTPS_PROXY": "http://proxy.evil:8080"}},
        {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x"}},
        {"env": {"CLAUDE_CONFIG_DIR": "C:/elsewhere"}},
        {"statusLine": {"type": "command", "command": "evil.exe"}},
        {"awsAuthRefresh": "evil"},
        {"awsCredentialExport": "evil"},
        {"otelHeadersHelper": "evil"},
        {"outputStyle": "pirate"},
        {"model": "some-model"},
        {"somethingNew": True},
        {"permissions": {"additionalDirectories": ["C:/"]}},
        {"permissions": {"defaultMode": "acceptEdits"}},
        {"permissions": "allow-all"},
    ],
)
def test_other_user_settings_exposures_are_fallback3(env, settings) -> None:  # noqa: ANN001
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    cfg = tmp / "cfg"
    cfg.mkdir()
    text = settings if isinstance(settings, str) else json.dumps(settings)
    (cfg / "settings.json").write_text(text, encoding="utf-8")
    runner, fake, _t = make_runner(db, tmp, claude_cfg=cfg)
    runner.run_once()
    assert _job_row(db)["error"] == "isolation_fallback3" and fake.calls == []


def test_harmless_settings_keys_are_clean(env) -> None:  # noqa: ANN001
    """allowlist의 무해 키·빈 hooks/env·꺼진 플러그인·deny 규칙은 깨끗하다(M-1)."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    cfg = tmp / "cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text(
        json.dumps(
            {
                "$schema": "https://json.schemastore.org/claude-code-settings.json",
                "cleanupPeriodDays": 30,
                "includeCoAuthoredBy": False,
                "hooks": {},
                "env": {},
                "enabledPlugins": {"x@market": False},
                "permissions": {"deny": ["Bash"], "ask": [], "defaultMode": "default"},
            }
        ),
        encoding="utf-8",
    )
    (cfg / "CLAUDE.md").write_text("내 메모", encoding="utf-8")  # 해시 대상일 뿐 사유 아님
    runner, _fake, _t = make_runner(db, tmp, claude_cfg=cfg)
    runner.run_once()
    assert _job_row(db)["outcome"] == "drafted"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("managed-settings.json", {"hooks": {"Stop": [{"hooks": []}]}}),
        ("managed-settings.json", {"env": {"HTTPS_PROXY": "http://p"}}),
        ("managed-settings.json", "broken{"),
        ("managed-mcp.json", {"mcpServers": {}}),
    ],
)
def test_managed_settings_exposures_are_fallback3(env, filename, content) -> None:  # noqa: ANN001
    """M-1: managed settings(정책 파일)도 같은 allowlist로 검사, managed-mcp는 있으면 fallback3."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    managed = tmp / "ProgramFiles" / "ClaudeCode"
    managed.mkdir(parents=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (managed / filename).write_text(text, encoding="utf-8")
    runner, fake, _t = make_runner(db, tmp, managed_dirs=(tmp / "absent", managed))
    runner.run_once()
    assert _job_row(db)["error"] == "isolation_fallback3" and fake.calls == []


def test_harmless_managed_settings_are_clean(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    managed = tmp / "ProgramData" / "ClaudeCode"
    managed.mkdir(parents=True)
    (managed / "managed-settings.json").write_text(
        json.dumps({"permissions": {"disableBypassPermissionsMode": "disable", "deny": ["Bash"]}}),
        encoding="utf-8",
    )
    runner, _fake, _t = make_runner(db, tmp, managed_dirs=(managed,))
    runner.run_once()
    assert _job_row(db)["outcome"] == "drafted"


# ---------------------------------------------------------------- H-1: CLAUDE_CONFIG_DIR


def _home_env(home: Path, **extra: str) -> dict[str, str]:
    key = "USERPROFILE" if sys.platform == "win32" else "HOME"
    return {"PATH": "", "SYSTEMROOT": r"C:\Windows", key: str(home), **extra}


def test_claude_config_dir_env_is_unsupported_fallback3(env) -> None:  # noqa: ANN001
    """Spinoza 30번 H-1: 원본 환경에 CLAUDE_CONFIG_DIR이 있으면 (그 디렉터리가 깨끗해도)
    자식이 읽는 경로와 어긋날 수 있으므로 claude를 실행하지 않고 fallback3으로 취소한다."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    clean_dir = tmp / "clean_cfg"
    clean_dir.mkdir()
    runner, fake, _t = make_runner(
        db,
        tmp,
        env_source=_home_env(tmp / "home", CLAUDE_CONFIG_DIR=str(clean_dir)),
        config_from_env=True,
    )
    runner.run_once()
    job = _job_row(db)
    assert (job["status"], job["error"]) == ("cancelled", "isolation_fallback3")
    assert fake.calls == []


def test_claude_config_dir_env_lowercase_also_detected(env) -> None:  # noqa: ANN001
    if sys.platform != "win32":
        pytest.skip("Windows 환경변수는 대소문자를 구분하지 않는다")
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    runner, fake, _t = make_runner(
        db,
        tmp,
        env_source=_home_env(tmp / "home", claude_config_dir=str(tmp / "x")),
        config_from_env=True,
    )
    runner.run_once()
    assert _job_row(db)["error"] == "isolation_fallback3" and fake.calls == []


def test_checked_config_dir_is_child_config_dir(env) -> None:  # noqa: ANN001
    """H-1: 점검 경로 = 자식 env에서 계산한 경로(자식 홈의 .claude). 자식 홈 쪽 settings에
    hooks가 있으면 fallback3, 없으면 실행되고 자식 env에는 CLAUDE_CONFIG_DIR이 없다."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    home = tmp / "home"
    (home / ".claude").mkdir(parents=True)
    source = _home_env(home)
    assert preflight.claude_config_dir(source) == home / ".claude"

    _queue_job(db, account, tmp)
    runner, fake, _t = make_runner(db, tmp, env_source=source, config_from_env=True)
    runner.run_once()
    assert _job_row(db)["outcome"] == "drafted"
    child_env = fake.calls[0]["env"]
    assert "CLAUDE_CONFIG_DIR" not in child_env
    assert preflight.claude_config_dir(child_env) == preflight.claude_config_dir(source)

    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": []}]}}), encoding="utf-8"
    )
    mid = store(db, tmp / "mail", account, eml())
    engine = make_engine(db)
    engine.start(2)
    engine.evaluate_message(mid, expected_generation=2)
    runner.run_once()
    assert _job_row(db)["error"] == "isolation_fallback3" and len(fake.calls) == 1


# ---------------------------------------------------------------- M-2: 사후 점검 강제


def test_settings_changed_during_run_discards(env) -> None:  # noqa: ANN001
    """M-2: 실행 중 user 설정(또는 ~/.claude/CLAUDE.md)이 바뀌면 초안 없이 보안 폐기."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    cfg = tmp / "cfg"
    cfg.mkdir()
    runner, fake, _t = make_runner(db, tmp, claude_cfg=cfg)
    fake.on_spawn = lambda _cwd: (cfg / "CLAUDE.md").write_text("바뀜", encoding="utf-8")
    runner.run_once()
    job = _job_row(db)
    assert (job["status"], job["error"], job["error_class"]) == (
        "failed",
        "preflight_settings_changed",
        "security",
    )
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0
    autosend = query(db, "SELECT value_json FROM settings WHERE key LIKE '%autosend_state'")
    assert json.loads(autosend[0][0]) == "paused_security"


def test_unexpected_file_in_workdir_discards(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    runner, fake, _t = make_runner(db, tmp)
    fake.on_spawn = lambda cwd: (Path(cwd) / "dropped.txt").write_text("x", encoding="utf-8")
    runner.run_once()
    job = _job_row(db)
    assert (job["status"], job["error"]) == ("failed", "preflight_unexpected_files")
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0


def test_unclean_ancestor_cancels(env) -> None:  # noqa: ANN001
    """M-2: 잡 디렉터리 상위에 CLAUDE.md가 있으면 초안 없이 cancelled."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    (tmp / "CLAUDE.md").write_text("상위 지침", encoding="utf-8")
    runner, _fake, _t = make_runner(db, tmp)
    runner.run_once()
    job = _job_row(db)
    assert (job["status"], job["error"]) == ("cancelled", "preflight_ancestor_unclean")
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0


def test_mcp_only_allow_rules_are_clean(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    cfg = tmp / "cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["mcp__other__tool"]}}), encoding="utf-8"
    )
    runner, _fake, _t = make_runner(db, tmp, claude_cfg=cfg)
    runner.run_once()
    assert _job_row(db)["outcome"] == "drafted"


@pytest.mark.parametrize(
    ("scenario", "reason", "error_class"),
    [
        ("extra_tool", "security_tool", "security"),
        ("no_submit", "submit_count", "permanent"),
        ("double_submit", "security_submit_count", "security"),  # Spinoza 30번 L-1
        ("exit_error", "claude_exit", "permanent"),
        ("timeout", "timeout", "transient"),
        ("timeout_extra_tool", "security_tool", "security"),  # L-1: 타임아웃도 stdout 먼저 판정
    ],
)
def test_post_run_verification_failures_discard(env, scenario, reason, error_class) -> None:  # noqa: ANN001
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    runner, fake, tokens = make_runner(db, tmp)
    fake.scenario = scenario
    runner.run_once()
    job = _job_row(db)
    assert (job["status"], job["error"], job["error_class"]) == ("failed", reason, error_class)
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0
    assert tokens.active_job_ids() == frozenset()
    autosend = query(db, "SELECT value_json FROM settings WHERE key LIKE '%autosend_state'")
    if error_class == "security":
        assert json.loads(autosend[0][0]) == "paused_security"
    if scenario.startswith("timeout"):
        assert "kill" in fake.events


def test_skip_submission_records_skipped(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    runner, fake, _t = make_runner(db, tmp)
    fake.scenario = "skip"
    runner.run_once()
    assert _job_row(db)["outcome"] == "skipped"
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0


def test_unpinned_or_changed_cli_fails_without_spawn(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    _queue_job(db, account, tmp)
    runner, fake, _t = make_runner(db, tmp)
    runner.run_once()
    assert _job_row(db)["error"] == "claude_cli_unavailable" and fake.calls == []
    exe = pin_fake_cli(db, tmp)
    exe.write_bytes(b"tampered")  # 해시 변경 → 재확인 필요
    mid = store(db, tmp / "mail", account, eml())
    engine = make_engine(db)
    engine.start(2)
    engine.evaluate_message(mid, expected_generation=2)
    runner.run_once()
    assert _job_row(db)["error"] == "claude_cli_unavailable" and fake.calls == []


def test_no_claim_while_mcp_down(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    runner, _fake, _t = make_runner(db, tmp, port=None)
    assert runner.run_once() is False
    assert _job_row(db)["status"] == "queued"


def test_abort_revokes_token_before_kill() -> None:
    events: list[str] = []
    tokens = RecordingTokens(events)

    class _Proc:
        def kill_tree(self, handle: int) -> None:
            events.append(f"kill:{handle}")

    from emailtomcp.autoreply.job_runner import JobRunner

    runner = JobRunner.__new__(JobRunner)  # 스레드·DB 없이 abort 순서만 본다
    import threading

    runner._tokens = tokens
    runner._proc = _Proc()
    runner._lock = threading.Lock()
    runner._aborted = set()
    runner._current_job = 7
    runner._current_handle = 1234
    runner.abort_jobs([7])
    assert events == ["revoke:7", "kill:1234"]


def test_controller_disable_order_db_then_unsubscribe_then_revoke_kill(env) -> None:  # noqa: ANN001
    """끄기 순서(§7.0, S-1): DB 커밋 → 구독 해제 → (러너) 토큰 폐기 → kill → 러너 정지."""
    db, account, tmp = env
    add_rule(db)
    events: list[str] = []

    def db_enabled() -> bool:
        conn = db.new_read_connection()
        try:
            from emailtomcp.storage.repositories import autoreply as ar

            return ar.get_toggle_state(conn).enabled
        finally:
            conn.close()

    class _Engine:
        def start(self, generation: int) -> None:
            events.append("engine.start")

        def stop(self) -> None:
            events.append(f"engine.stop(db_enabled={db_enabled()})")

        def catch_up(self, *, expected_generation: int) -> int:
            events.append("engine.catch_up")
            return 0

    class _Runner:
        def start(self) -> None:
            events.append("runner.start")

        def wake(self) -> None:
            events.append("runner.wake")

        def abort_jobs(self, ids) -> None:  # noqa: ANN001
            events.append(f"runner.abort({list(ids)})")

        def stop(self) -> None:
            events.append("runner.stop")

    controller = AutoReplyController(
        db, clock=SystemClock(), event_bus=EventBus(), engine=_Engine(), runner=_Runner()
    )

    holder: dict[str, int] = {}

    async def scenario() -> None:
        await controller.enable(expected_generation=1)
        # 실행 중 잡을 하나 만든다(끄기 드레인이 running 잡 ID를 돌려주게)
        engine = make_engine(db)
        engine.start(2)
        mid = store(db, tmp / "mail", account, eml())
        engine.evaluate_message(mid, expected_generation=2)
        from emailtomcp.storage.repositories import autoreply as ar

        job = ar.claim_next_job(db, now=datetime.now(UTC))
        assert job is not None
        holder["job"] = job.id
        await controller.disable()

    asyncio.run(scenario())
    assert events[:4] == ["runner.start", "engine.start", "engine.catch_up", "runner.wake"]
    assert events[4:] == [
        "engine.stop(db_enabled=False)",
        f"runner.abort([{holder['job']}])",
        "runner.stop",
    ]
    assert query(db, "SELECT status FROM auto_reply_jobs")[0][0] == "cancelled"


def test_off_toggle_means_no_engine_or_runner_start(env) -> None:  # noqa: ANN001
    db, _a, _t = env
    events: list[str] = []

    class _Spy:
        def __getattr__(self, name: str):  # noqa: ANN204
            return lambda *a, **k: events.append(name)

    controller = AutoReplyController(
        db, clock=SystemClock(), event_bus=EventBus(), engine=_Spy(), runner=_Spy()
    )
    asyncio.run(controller.start_if_enabled())
    assert events == []  # I2: off면 구독·러너·따라잡기 0회


def test_end_to_end_message_to_draft(env) -> None:  # noqa: ANN001
    """MessageReceived → RuleEngine(G2·G3) → 러너(G4~G7) → draft."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    add_rule(db)
    gen = enable(db)
    bus = EventBus()
    runner, _fake, _t = make_runner(db, tmp)
    engine = make_engine(db, bus, on_job=runner.wake)
    engine.start(gen)
    mid = store(db, tmp / "mail", account, eml())
    bus.publish("MessageReceived", {"message_id": mid, "is_backfill": False})
    assert runner.run_once() is True
    assert query(db, "SELECT auto_reply_status FROM messages WHERE id=?", (mid,))[0][0] == "drafted"
    assert runner.run_once() is False


# ---------------------------------------------------------------- L-4: 실행 중 규칙·계정 변경


@pytest.mark.parametrize("change", ["delete", "disable_rule", "disable_account"])
def test_revalidate_aborts_running_claude(env, change) -> None:  # noqa: ANN001
    """데카르트 31번 L-4: 실행 중 규칙 삭제·중지·계정 비활성 커밋 뒤 revalidate_current가
    DB를 cancelled로 두고 토큰 폐기 → 프로세스 kill 순서로 정리한다."""
    from emailtomcp.storage.repositories import accounts as accounts_repo
    from emailtomcp.storage.repositories import rules as rules_repo

    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    rule_id = query(db, "SELECT id FROM rules")[0][0]
    events: list[str] = []
    tokens = RecordingTokens(events)
    runner, fake, _t = make_runner(db, tmp, tokens=tokens)
    fake.events = events
    results: list[bool] = []

    def mutate(_cwd) -> None:  # noqa: ANN001
        if change == "delete":
            rules_repo.delete_rule(db, rule_id)
        elif change == "disable_rule":
            rules_repo.set_rule_enabled(db, rule_id, False)
        else:
            accounts_repo.update_account(db, account, auto_reply_enabled=False)
        events.clear()
        results.append(runner.revalidate_current())

    fake.on_spawn = mutate
    outcome_before = runner.run_once()
    assert outcome_before is True and results == [True]
    job = _job_row(db)
    expected = {
        "delete": "rule_deleted",
        "disable_rule": "rule_disabled",
        "disable_account": "account_disabled",
    }[change]
    assert (job["status"], job["error"]) == ("cancelled", expected)
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0
    job_id = job["id"]
    assert events.index(f"revoke:{job_id}") < events.index("kill")


def test_revalidate_keeps_valid_running_job(env) -> None:  # noqa: ANN001
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    runner, fake, _t = make_runner(db, tmp)
    results: list[bool] = []
    fake.on_spawn = lambda _cwd: results.append(runner.revalidate_current())
    runner.run_once()
    assert results == [False]
    assert _job_row(db)["outcome"] == "drafted"
    assert runner.revalidate_current() is False  # 실행 중 잡 없음


# ---------------------------------------------------------------- N-3: 중지와 보안 판정 순서


def _autosend_state(db):  # noqa: ANN001, ANN202
    rows = query(db, "SELECT value_json FROM settings WHERE key LIKE '%autosend_state'")
    return json.loads(rows[0][0]) if rows else None


@pytest.mark.parametrize("scenario", ["extra_tool", "double_submit"])
def test_abort_does_not_hide_security_signal(env, scenario) -> None:  # noqa: ANN001
    """Spinoza 34번 N-3: 사용자 중지(abort)와 허용 외 도구 시도가 겹쳐도 보안 판정이 먼저다."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    runner, fake, _t = make_runner(db, tmp)
    fake.scenario = scenario
    fake.on_spawn = lambda _cwd: runner.abort_jobs([runner._current_job])
    runner.run_once()
    job = _job_row(db)
    expected = "security_tool" if scenario == "extra_tool" else "security_submit_count"
    assert (job["status"], job["error"], job["error_class"]) == ("failed", expected, "security")
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0
    assert _autosend_state(db) == "paused_security"


def test_security_signal_kept_when_job_already_cancelled(env) -> None:  # noqa: ANN001
    """N-3: 끄기·규칙 변경으로 DB가 먼저 cancelled된 잡도 보안 신호는 paused_security로 남는다."""
    from emailtomcp.storage.repositories import rules as rules_repo

    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    rule_id = query(db, "SELECT id FROM rules")[0][0]
    runner, fake, _t = make_runner(db, tmp)
    fake.scenario = "extra_tool"

    def mutate(_cwd) -> None:  # noqa: ANN001
        rules_repo.set_rule_enabled(db, rule_id, False)
        assert runner.revalidate_current() is True

    fake.on_spawn = mutate
    runner.run_once()
    job = _job_row(db)
    assert (job["status"], job["error"]) == ("cancelled", "rule_disabled")
    assert _autosend_state(db) == "paused_security"


def test_abort_without_security_signal_is_plain_abort(env) -> None:  # noqa: ANN001
    """N-3 반대쪽: 허용 도구만 썼으면 중지는 그냥 중지(보안 정지 없음)."""
    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    runner, fake, _t = make_runner(db, tmp)
    fake.on_spawn = lambda _cwd: runner.abort_jobs([runner._current_job])
    runner.run_once()
    assert query(db, "SELECT COUNT(*) FROM drafts")[0][0] == 0
    assert _autosend_state(db) != "paused_security"


def test_draft_guard_findings_for_compose_banner(env) -> None:  # noqa: ANN001
    """데카르트 31번 M-3: 작성 창 배너 근거 — 자동회신 초안이면 G7 가드 findings, 아니면 None."""
    from emailtomcp.storage.repositories import autoreply as ar

    db, account, tmp = env
    pin_fake_cli(db, tmp)
    _queue_job(db, account, tmp)
    runner, fake, _t = make_runner(db, tmp)
    fake.body = "확인했습니다. https://pay.evil.example 에서 1,500,000원을 입금해 주세요."
    runner.run_once()
    draft_id = _job_row(db)["result_draft_id"]
    assert draft_id is not None
    conn = db.new_read_connection()
    try:
        findings = ar.get_draft_guard_findings(conn, draft_id)
        assert findings is not None and {"url", "money"} <= set(findings)
        conn.execute(
            "INSERT INTO drafts (account_id, kind, origin, status, created_at, updated_at) "
            "VALUES (?, 'new', 'user', 'draft', 'x', 'x')",
            (account,),
        )
        user_draft = conn.execute("SELECT MAX(id) FROM drafts").fetchone()[0]
        assert ar.get_draft_guard_findings(conn, user_draft) is None
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["claude", "fixed_template"])
def test_autoreply_draft_source_distinguishes_claude_and_template(env, mode) -> None:  # noqa: ANN001
    """데카르트 33번 I-4: 작성 창 문구 근거 — G7 기록의 claude 실행 여부로 구분한다."""
    from emailtomcp.storage.repositories import autoreply as ar

    db, account, tmp = env
    if mode == "claude":
        pin_fake_cli(db, tmp)
        _queue_job(db, account, tmp)
    else:
        _queue_job(
            db,
            account,
            tmp,
            reply_mode="fixed_template",
            fixed_template="확인 후 연락드리겠습니다.",
        )
    runner, _fake, _t = make_runner(db, tmp)
    runner.run_once()
    draft_id = _job_row(db)["result_draft_id"]
    assert draft_id is not None
    conn = db.new_read_connection()
    try:
        assert ar.get_autoreply_draft_source(conn, draft_id) == mode
        conn.execute(
            "INSERT INTO drafts (account_id, kind, origin, status, created_at, updated_at) "
            "VALUES (?, 'new', 'user', 'draft', 'x', 'x')",
            (account,),
        )
        user_draft = conn.execute("SELECT MAX(id) FROM drafts").fetchone()[0]
        assert ar.get_autoreply_draft_source(conn, user_draft) is None
        conn.execute(
            "INSERT INTO drafts (account_id, kind, origin, status, created_at, updated_at) "
            "VALUES (?, 'reply', 'autoreply', 'draft', 'x', 'x')",
            (account,),
        )
        orphan = conn.execute("SELECT MAX(id) FROM drafts").fetchone()[0]
        assert ar.get_autoreply_draft_source(conn, orphan) == "unknown"
        conn.rollback()
    finally:
        conn.close()
