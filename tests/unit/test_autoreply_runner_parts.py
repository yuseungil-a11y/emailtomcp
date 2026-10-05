"""러너 구성요소 단위 테스트: env allowlist(C-8), stream-json 파서(C-10), 프롬프트(C-2·H7-4),
출력 가드(§7.5), 사전점검(§2(c) 폴백), CLI 고정(§9.3, C-3), 잡 디렉터리(H4) — P3 Phase B."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from emailtomcp.autoreply import cli_locator, job_dir, preflight, prompts
from emailtomcp.autoreply.claude_runner import parse_stream
from emailtomcp.autoreply.env_policy import JOB_TOKEN_ENV, build_child_env
from emailtomcp.rules import output_guard

pytestmark = pytest.mark.unit


def test_child_env_is_allowlist_only(tmp_path: Path) -> None:
    source = {
        "PATH": r"C:\Users\me\Downloads;C:\evil",
        "SYSTEMROOT": r"C:\Windows",
        "HOME": "/home/me",
        "USERPROFILE": r"C:\Users\me",
        "LANG": "ko_KR.UTF-8",
        "LC_ALL": "ko_KR.UTF-8",
        "ANTHROPIC_API_KEY": "sk-secret",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "NODE_OPTIONS": "--require evil.js",
        "AWS_SECRET_ACCESS_KEY": "x",
        "HTTPS_PROXY": "http://proxy",
    }
    env = build_child_env(source, job_tmp=tmp_path / "tmp", token="ejob.T", program_dirs=[tmp_path])
    assert env[JOB_TOKEN_ENV] == "ejob.T"
    assert env["TEMP"] == env["TMP"] == str(tmp_path / "tmp")
    for banned in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "NODE_OPTIONS",
        "AWS_SECRET_ACCESS_KEY",
        "HTTPS_PROXY",
    ):
        assert banned not in env
    assert env["LANG"] == "ko_KR.UTF-8" and env["LC_ALL"] == "ko_KR.UTF-8"
    assert "Downloads" not in env["PATH"] and "evil" not in env["PATH"]
    assert env["PATH"].split(";" if sys.platform == "win32" else ":")[0] == str(tmp_path)


def test_parse_stream_counts_tools_and_requires_result() -> None:
    lines = [
        {"type": "system", "subtype": "init", "tools": ["mcp__emailtomcp__get_job_message"]},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "name": "mcp__emailtomcp__get_job_message"},
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "mcp__emailtomcp__submit_auto_reply"}]
            },
        },
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3},
    ]
    data = "\n".join(json.dumps(x) for x in lines).encode() + b"\nnot json\n"
    report = parse_stream(data)
    assert report.tools_used == (
        "mcp__emailtomcp__get_job_message",
        "mcp__emailtomcp__submit_auto_reply",
    )
    assert report.submit_count == 1 and report.turns == 3 and report.result_seen
    assert not report.is_error and report.unparsed_lines == 1
    no_result = parse_stream(json.dumps(lines[1]).encode())
    assert not no_result.result_seen
    max_turns = parse_stream(json.dumps({"type": "result", "subtype": "error_max_turns"}).encode())
    assert max_turns.is_error


def test_prompt_and_template_rendering() -> None:
    prompt = prompts.build_user_prompt(
        account_name="업무",
        rule_name="${extra_instructions}",
        extra_instructions="$max_chars",
        max_chars=300,
    )
    assert "${extra_instructions}" in prompt and "$max_chars" in prompt  # 재해석 없음
    assert "300자" in prompt
    assert prompts.sender_name_safe("김철수\u202e") == "김철수"
    assert prompts.sender_name_safe("click http://evil.example") == ""
    assert prompts.sender_name_safe("me@evil.example") == ""
    assert prompts.sender_name_safe("가" * 60) == "가" * 40
    body = prompts.render_fixed_template(
        "{sender_name_safe}님 {received_date} {account_name} {unknown}",
        sender_name="김철수",
        received_date="2026-10-05",
        account_name="업무",
    )
    assert body == "김철수님 2026-10-05 업무 {unknown}"
    assert prompts.make_reply_subject("RE: Re: 회신: 견적\r\n문의") == "Re: 견적 문의"
    assert prompts.make_reply_subject(None) == "Re:"
    assert "\n" not in prompts.SYSTEM_PROMPT.split("\n")[0]


@pytest.mark.parametrize(
    ("body", "finding"),
    [
        ("자세한 내용은 hxxps://evil[.]com 참고", "url"),
        ("www . example . com 방문", "url"),
        ("메일은 kim [at] example.com 으로", "email"),
        ("연락처 010-1234-5678", "phone"),
        ("+82 10 1234 5678로 전화", "phone"),
        ("계좌 123-4567-8901-23", "account_number"),
        ("총 1,500,000원 입금", "money"),
        ("1,500,000원을 입금해 주세요", "money"),  # 조사가 바로 붙은 한글 단위
        ("100만원입니다", "money"),
        ("300달러를 보내 주세요", "money"),
        ("20USD.", "money"),
        ("견적 승인 확정해 드립니다", "promise"),
        ("> 원문 인용\n답장입니다", "quote_line"),
    ],
)
def test_output_guard_detects(body: str, finding: str) -> None:
    result = output_guard.check(
        body=body, subject="Re: x", original_body="", original_subject="x", max_chars=400
    )
    assert finding in result.findings and not result.ok


def _money(body: str) -> bool:
    result = output_guard.check(
        body=body, subject="Re: x", original_body="", original_subject="x", max_chars=400
    )
    return "money" in result.findings


@pytest.mark.parametrize(
    "body",
    [
        # 데카르트 33번 L-8: 번호 목록·'원'으로 시작하는 낱말 오탐
        "1. 원인 분석",
        "2. 원본 파일",
        "3. 원활한 진행",
        "제3원칙",
        "4원소",
        "2원화",
        "회의실 3 원격",
        "버전2.0원문",
        "2억년",
        "3억제",
        "10월3일원격회의",
        "회원10명",
        "불만 원인 분석",
        "사원 3명",
        "100만 명",
        "조원들에게 공유",
    ],
)
def test_output_guard_money_no_false_positive(body: str) -> None:
    assert not _money(body)


@pytest.mark.parametrize(
    "body",
    [
        # Spinoza 34번 N-4: 놓치던 표현
        "100만 원",
        "100만",
        "일백만원",
        "백만원",
        "오십만 원",
        "금일백만원정",
        "1백만원",
        "2억 원",
        # 기존에 잡던 금액(회귀 방지)
        "1,500,000원을",
        "100만원입니다",
        "50달러를",
        "5천원",
        "100usd",
        "1500원인데",
        "₩5000",
    ],
)
def test_output_guard_money_detects(body: str) -> None:
    assert _money(body)


def test_output_guard_clean_and_original_copy() -> None:
    clean = output_guard.check(
        body="문의 감사합니다. 확인 후 다시 연락드리겠습니다.",
        subject="Re: x",
        original_body="견적 문의",
        original_subject="x",
        max_chars=400,
    )
    assert clean.ok and clean.findings == ()
    original = "가나다라마바사아자차카타파하 " * 5
    copied = output_guard.check(
        body="답장: " + original,
        subject="Re",
        original_body=original,
        original_subject=None,
        max_chars=1000,
    )
    assert "original_copy" in copied.findings
    assert (
        copied.body_sha256
        == __import__("hashlib").sha256(("답장: " + original).encode()).hexdigest()
    )


def test_user_settings_inspection(tmp_path: Path) -> None:
    cfg = tmp_path / "c"
    cfg.mkdir()
    findings, digest = preflight.inspect_user_settings(cfg, managed_dirs=())
    assert findings == []  # 파일 없음 = 깨끗
    (cfg / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 9}), encoding="utf-8")
    assert preflight.inspect_user_settings(cfg, managed_dirs=())[0] == []
    # allowlist 전환(Spinoza 30번 M-1): 허용 목록 밖 최상위 키(model 등)는 사유가 된다.
    (cfg / "settings.json").write_text(json.dumps({"model": "x"}), encoding="utf-8")
    assert preflight.inspect_user_settings(cfg, managed_dirs=())[0] == ["key:settings.json:model"]
    (cfg / "settings.json").write_text(json.dumps({}), encoding="utf-8")
    (cfg / "settings.local.json").write_text(json.dumps({"hooks": {"Stop": [1]}}), encoding="utf-8")
    findings2, digest2 = preflight.inspect_user_settings(cfg, managed_dirs=())
    assert findings2 == ["hooks:settings.local.json"] and digest2 != digest


def test_user_memory_and_managed_files_are_hashed(tmp_path: Path) -> None:
    """M-1·M-2: ~/.claude/CLAUDE.md와 managed 파일도 해시 대상(사후 변경 감지)."""
    cfg = tmp_path / "c"
    managed = tmp_path / "m"
    cfg.mkdir()
    managed.mkdir()
    base = preflight.inspect_user_settings(cfg, managed_dirs=(managed,))
    (cfg / "CLAUDE.md").write_text("메모", encoding="utf-8")
    with_memory = preflight.inspect_user_settings(cfg, managed_dirs=(managed,))
    assert with_memory[0] == [] and with_memory[1] != base[1]  # 사유는 아니고 해시만 바뀜
    (managed / "managed-settings.json").write_text(json.dumps({}), encoding="utf-8")
    with_managed = preflight.inspect_user_settings(cfg, managed_dirs=(managed,))
    assert with_managed[0] == [] and with_managed[1] != with_memory[1]
    (managed / "managed-mcp.json").write_text("{}", encoding="utf-8")
    assert preflight.inspect_user_settings(cfg, managed_dirs=(managed,))[0] == [
        "managed_mcp:managed0:managed-mcp.json"
    ]


def test_config_dir_is_computed_from_child_env_mapping(tmp_path: Path) -> None:
    """H-1: 점검 경로는 build_child_env와 같은 Mapping에서 계산되고, 원본과 자식 env 어느 쪽으로
    계산해도 같다. CLAUDE_CONFIG_DIR은 따르지도, 자식에게 넘기지도 않는다."""
    home_key = "USERPROFILE" if sys.platform == "win32" else "HOME"
    source = {
        home_key: str(tmp_path / "home"),
        "SYSTEMROOT": r"C:\Windows",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "elsewhere"),
    }
    child = build_child_env(source, job_tmp=tmp_path / "t", token="ejob.T", program_dirs=[])
    assert "CLAUDE_CONFIG_DIR" not in child
    assert preflight.claude_config_dir(source) == tmp_path / "home" / ".claude"
    assert preflight.claude_config_dir(child) == preflight.claude_config_dir(source)


def test_pre_checks_flag_claude_config_dir_env(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    jd = job_dir.create_job_dir(tmp_path / "jobs", job_id=1, attempt=1)
    try:
        home_key = "USERPROFILE" if sys.platform == "win32" else "HOME"
        clean = {home_key: str(tmp_path / "home")}
        pre = preflight.run_pre_checks(
            job_id=1, attempt=1, job_dir=jd, now=datetime.now(UTC),
            env_source=clean, managed_dirs=(),
        )  # fmt: skip
        assert pre.isolation_mode == "user_settings_clean"
        assert pre.config_dir == tmp_path / "home" / ".claude"
        flagged = preflight.run_pre_checks(
            job_id=1, attempt=1, job_dir=jd, now=datetime.now(UTC),
            env_source={**clean, "CLAUDE_CONFIG_DIR": str(tmp_path / "home" / ".claude")},
            managed_dirs=(),
        )  # fmt: skip
        assert flagged.isolation_mode == "fallback3"
        assert "unsupported_env:CLAUDE_CONFIG_DIR" in flagged.isolation_findings
    finally:
        job_dir.remove_job_dir(jd)


def test_pre_checks_flag_missing_home_key(tmp_path: Path) -> None:
    """Spinoza 34번 N-1: 자식에게 넘길 홈 키가 없으면(점검 경로와 자식 홈이 어긋날 수 있음)
    fallback3(`unsupported_env:home_missing`). 다른 쪽 플랫폼의 홈 키만 있어도 마찬가지."""
    from datetime import UTC, datetime

    jd = job_dir.create_job_dir(tmp_path / "jobs", job_id=2, attempt=1)
    try:
        other_key = "HOME" if sys.platform == "win32" else "USERPROFILE"
        for source in ({}, {"SYSTEMROOT": r"C:\Windows"}, {other_key: str(tmp_path / "home")}):
            pre = preflight.run_pre_checks(
                job_id=2, attempt=1, job_dir=jd, now=datetime.now(UTC),
                env_source=source, managed_dirs=(),
            )  # fmt: skip
            assert pre.isolation_mode == "fallback3", source
            assert preflight.UNSUPPORTED_HOME_MISSING in pre.isolation_findings
            assert preflight.UNSUPPORTED_HOME_MISSING == "unsupported_env:home_missing"
        home_key = "USERPROFILE" if sys.platform == "win32" else "HOME"
        ok = preflight.run_pre_checks(
            job_id=2, attempt=1, job_dir=jd, now=datetime.now(UTC),
            env_source={home_key: str(tmp_path / "home")}, managed_dirs=(),
        )  # fmt: skip
        assert preflight.UNSUPPORTED_HOME_MISSING not in ok.isolation_findings
    finally:
        job_dir.remove_job_dir(jd)


def test_allowed_tools_constant_is_shared_with_core() -> None:
    """Spinoza 34번 L-5: 러너(G6)와 판정기(G7)가 같은 허용 도구 집합을 쓴다."""
    from emailtomcp.autoreply import env_policy
    from emailtomcp.core.autoreply_types import AUTOREPLY_DRAFT_ALLOWED_TOOLS

    assert env_policy.ALLOWED_TOOLS_AUTO_REPLY_DRAFT is AUTOREPLY_DRAFT_ALLOWED_TOOLS
    assert AUTOREPLY_DRAFT_ALLOWED_TOOLS == (
        env_policy.TOOL_PREFIX + env_policy.TOOL_GET_JOB_MESSAGE,
        env_policy.TOOL_PREFIX + env_policy.TOOL_SUBMIT_AUTO_REPLY,
    )


def test_ancestor_markers(tmp_path: Path) -> None:
    work = tmp_path / "a" / "b" / "work"
    work.mkdir(parents=True)
    # 실제 홈의 ~/.claude는 알려진 user 경로라 제외된다(이 PC 기준 기준선).
    baseline = preflight.ancestors_clean(work)
    (tmp_path / "a" / ".claude").mkdir()
    assert not preflight.ancestors_clean(work)
    (tmp_path / "a" / ".claude").rmdir()
    assert preflight.ancestors_clean(work) == baseline
    (tmp_path / "a" / "CLAUDE.md").write_text("x", encoding="utf-8")
    assert not preflight.ancestors_clean(work)


def test_job_dir_config_has_no_token_value(tmp_path: Path) -> None:
    jd = job_dir.create_job_dir(tmp_path / "jobs", job_id=3, attempt=1)
    assert jd.fresh and jd.acl == "owner_only"
    path = job_dir.write_mcp_config(jd, port=8765, token_env=JOB_TOKEN_ENV)
    config = json.loads(path.read_text(encoding="utf-8"))
    header = config["mcpServers"]["emailtomcp"]["headers"]["Authorization"]
    assert header == "Bearer ${EMAILTOMCP_JOB_TOKEN}"
    assert config["mcpServers"]["emailtomcp"]["url"] == "http://127.0.0.1:8765/mcp"
    with pytest.raises(FileExistsError):
        job_dir.write_mcp_config(jd, port=1, token_env=JOB_TOKEN_ENV)  # O_EXCL
    assert job_dir.remove_job_dir(jd) and not jd.root.exists()


def test_cli_pin_rules(tmp_path: Path) -> None:
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    with pytest.raises(ValueError):
        cli_locator.make_pin(str(shim), None)  # C-3: cmd 경유 금지
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"x")
    with pytest.raises(ValueError):
        cli_locator.make_pin(str(exe), None)  # 임시 폴더 아래는 거부
    pin = cli_locator.PinnedCli(
        program=exe, script=None, program_sha256=cli_locator.file_sha256(exe), script_sha256=None
    )
    assert cli_locator.verify_pin(pin) is None
    exe.write_bytes(b"changed")
    assert "다시 고정" in (cli_locator.verify_pin(pin) or "")
    assert cli_locator.verify_pin(None) is not None
    assert cli_locator.pin_from_setting({"program": 1}) is None
