"""잡 사전·사후 점검 (DESIGN.md §2(c) 설정 격리 폴백, §7.11 A8 PreflightResult) — P3 Phase B.

**PreflightResult는 이 모듈만 생성한다**(아키텍처 테스트). job_id·attempt·시각에 묶인다.

실행 전 점검(G4 커밋 후, 프로세스 시작 직전) — `run_pre_checks`
- jobdir_fresh / jobdir_acl: job_dir.create_job_dir 결과
- ancestor_clean: work 디렉터리의 상위 경로에 CLAUDE.md·CLAUDE.local.md·.claude가 없음
  (홈의 `~/.claude/`는 알려진 user 경로라 제외, H3-3·R1-9 — 대신 아래 user 설정 검사·해시 대상)
- isolation_mode:
  - R1-6(`--setting-sources project`가 user 설정을 배제함)이 **실측으로 확인되지 않았으므로**
    (docs/research에 R1 보고서 없음) 이번 단계는 언제나 user 설정을 읽기 전용으로 검사한다
    (§2(c) 설정 격리 폴백 1).
  - 검사는 **allowlist 방식**이다(Spinoza 30번 M-1): 무해하다고 명시한 최상위 키만 허용하고,
    그 밖의 키가 하나라도 있거나, `env` 블록이 비어 있지 않거나, hooks·활성 플러그인·MCP 외
    허용 규칙·허용 밖 권한 모드가 있거나, **읽을 수 없으면** `fallback3`. 깨끗하면
    `user_settings_clean`.
  - 점검 대상 = 자식 claude가 실제로 읽는 경로(H-1): user 설정 디렉터리는 `build_child_env`와
    같은 Mapping에서 계산한다(자식 홈 + `.claude`). 원본 환경에 `CLAUDE_CONFIG_DIR`이 있으면
    (자식에게는 넘기지 않으므로) 지원하지 않는 구성으로 보고 `fallback3`. 자식에게 넘길 홈 키
    (Windows=USERPROFILE, 그 밖=HOME)가 원본 환경에 없을 때도 점검 경로(Path.home())와 자식의
    실제 홈이 어긋날 수 있어 `fallback3`(`unsupported_env:home_missing`, Spinoza 34번 N-1).
  - managed settings(관리자 정책 파일: Windows `C:\\Program Files\\ClaudeCode\\`, 구버전
    `C:\\ProgramData\\ClaudeCode\\`)도 같은 allowlist로 검사하고, `managed-mcp.json`이 있으면
    `fallback3`.
  - Phase B 정책: `fallback3`이면 러너가 claude를 실행하지 않고 잡을 cancelled로 끝낸다
    (사용자 지시 — 설계서의 "auto_send만 금지, draft 허용"보다 보수적).
- user_settings_sha256: 검사한 파일(user settings 2종 + `~/.claude/CLAUDE.md` + managed
  settings·managed-mcp) 내용의 해시(TOCTOU 완화용 사후 비교 근거)

실행 후 점검(프로세스 종료 직후, G7 전) — `finalize`
- 잡 디렉터리에 예상 밖 파일(.claude, CLAUDE*.md, settings*, work/ 안의 모든 항목)이 생겼는지
- user_settings_sha256이 실행 전과 같은지(사전점검과 **같은 대상**을 다시 해시)
- G6 결과(도구 목록, submit 횟수, turns, 종료 코드)와 verified_body_sha256(메모리 본문 해시가
  DB submission_sha256과 같을 때만 채움)
- 사후 점검 결과는 G7 판정기(rules/autosend_verify.py ④)가 강제한다(Spinoza 30번 M-2).
  실행 중에 설정을 바꿨다가 원복하는 경우는 해시로 잡지 못한다 — 같은 사용자 권한이 필요한
  위협이라 수용한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from emailtomcp.autoreply.env_policy import CONFIG_DIR_ENV, child_home_dir, config_dir_override
from emailtomcp.autoreply.job_dir import JobDir
from emailtomcp.core.autoreply_types import IsolationMode, PreflightResult

# R1 실측(§13.2 R1-6)으로 `--setting-sources project`의 user 설정 배제가 확인되면 True로 바꾼다.
# 확인 전에는 언제나 user 설정을 검사한다(폴백 1/3).
R1_SETTING_SOURCES_VERIFIED = False

# 자식에게 넘길 홈 키가 원본 환경에 없을 때의 사전점검 사유(Spinoza 34번 N-1).
UNSUPPORTED_HOME_MISSING = "unsupported_env:home_missing"

_SETTINGS_FILES = ("settings.json", "settings.local.json")
_USER_MEMORY_FILE = "CLAUDE.md"
_MANAGED_SETTINGS_FILE = "managed-settings.json"
_MANAGED_MCP_FILE = "managed-mcp.json"
_ANCESTOR_MARKERS = ("CLAUDE.md", "CLAUDE.local.md", ".claude")
_UNEXPECTED_NAMES = (".claude", "claude.md", "claude.local.md")

# 값과 무관하게 허용하는 최상위 키(명령 실행·권한·지침·모델·네트워크·env에 영향이 없는 것).
# 새 키를 허용하려면 보안 검토 후 여기에 추가한다. model·outputStyle·statusLine·apiKeyHelper·
# awsAuthRefresh·awsCredentialExport·otelHeadersHelper 등은 일부러 넣지 않았다(M-1).
_HARMLESS_KEYS = frozenset(
    {
        "$schema",
        "cleanupPeriodDays",
        "includeCoAuthoredBy",
        "autoUpdatesChannel",
        "spinnerTipsEnabled",
        "alwaysThinkingEnabled",
        "feedbackSurveyState",
        "disableAllHooks",  # true는 더 제한적, false는 기본값
    }
)
# 값이 "비어 있음/끔"일 때만 허용하는 키는 `_inspect_settings_data`에서 개별 검사한다:
# hooks(비어 있음), env(비어 있음), enabledPlugins(전부 false), permissions(하위 키 allowlist).
_PERMISSION_HARMLESS_KEYS = frozenset({"deny", "ask", "disableBypassPermissionsMode"})
_PERMISSION_MODES_OK = frozenset({"default", "plan", "dontAsk"})


@dataclass(frozen=True, slots=True)
class PreCheck:
    job_id: int
    attempt: int
    pre_checked_at: str
    jobdir_fresh: bool
    jobdir_acl: str
    ancestor_clean: bool
    isolation_mode: IsolationMode
    isolation_findings: tuple[str, ...]
    user_settings_sha256: str
    # 사후 점검이 같은 대상을 다시 해시하도록 사전점검 대상을 그대로 기록한다.
    config_dir: Path
    managed_dirs: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class RunFacts:
    """G6 사후 검증 근거(러너가 stream-json에서 뽑은 값)."""

    tools_used: tuple[str, ...]
    submit_count: int
    turns: int | None
    exit_code: int | None


def claude_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """자식 claude가 실제로 읽는 user 설정 디렉터리(H-1).

    `env`에는 `build_child_env`에 넣을 원본 환경과 그 결과(자식 환경) 어느 쪽을 넣어도 같은
    값이 나온다 — 자식에게 넘기는 홈 키(Windows=USERPROFILE, 그 밖=HOME)만 본다.
    `CLAUDE_CONFIG_DIR`은 자식에게 넘기지 않으므로 여기서도 따르지 않는다(대신 사전점검이
    그 변수가 있으면 fallback3으로 처리한다).
    """
    source = env if env is not None else os.environ
    home = child_home_dir(source)
    return (home if home is not None else Path.home()) / ".claude"


def default_managed_dirs() -> tuple[Path, ...]:
    """managed settings(관리자 정책 파일) 위치 — `--setting-sources`로도 배제되지 않는다."""
    if sys.platform == "win32":
        return (Path(r"C:\Program Files\ClaudeCode"), Path(r"C:\ProgramData\ClaudeCode"))
    if sys.platform == "darwin":
        return (Path("/Library/Application Support/ClaudeCode"),)
    return (Path("/etc/claude-code"),)


def _short(key: Any) -> str:
    return "".join(ch for ch in str(key) if ch.isprintable())[:40]


def _inspect_permissions(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"permissions:{label}"]
    findings: list[str] = []
    for key, item in value.items():
        if key in _PERMISSION_HARMLESS_KEYS:
            continue
        if key == "allow":
            if not isinstance(item, list) or any(
                not (isinstance(rule, str) and rule.startswith("mcp__")) for rule in item
            ):
                findings.append(f"permissions_allow:{label}")
        elif key == "defaultMode":
            if item == "bypassPermissions":
                findings.append(f"bypass_permissions:{label}")
            elif item not in _PERMISSION_MODES_OK:
                findings.append(f"permissions_mode:{label}")
        else:
            findings.append(f"permissions_key:{label}:{_short(key)}")
    return findings


def _inspect_settings_data(data: dict[str, Any], label: str) -> list[str]:
    """allowlist 검사. 허용 밖이면 사유를 낸다(사유가 하나라도 있으면 fallback3)."""
    findings: list[str] = []
    for key, value in data.items():
        if key in _HARMLESS_KEYS:
            continue
        if key == "hooks":
            if value:
                findings.append(f"hooks:{label}")
        elif key == "env":
            if value:  # 비어 있지 않은 env 블록은 자식 env allowlist를 되돌린다
                findings.append(f"env:{label}")
        elif key == "enabledPlugins":
            plugins_on = (isinstance(value, dict) and any(bool(v) for v in value.values())) or (
                not isinstance(value, dict) and bool(value)
            )
            if plugins_on:
                findings.append(f"plugins:{label}")
        elif key == "permissions":
            findings.extend(_inspect_permissions(value, label))
        else:
            findings.append(f"key:{label}:{_short(key)}")
    return findings


def _hash_file(digest: Any, path: Path, label: str) -> bytes | None:
    """파일 내용을 해시에 넣고 내용을 돌려준다. 없으면 None, 못 읽으면 OSError."""
    digest.update(label.encode("utf-8") + b"\0")
    if not path.exists():
        digest.update(b"<absent>")
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        digest.update(b"<unreadable>")
        raise
    digest.update(raw)
    return raw


def _inspect_settings_file(digest: Any, path: Path, label: str, findings: list[str]) -> None:
    try:
        raw = _hash_file(digest, path, label)
    except OSError:
        findings.append(f"unreadable:{label}")
        return
    if raw is None:
        return
    try:
        data: Any = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        findings.append(f"unreadable:{label}")
        return
    if not isinstance(data, dict):
        findings.append(f"unreadable:{label}")
        return
    findings.extend(_inspect_settings_data(data, label))


def inspect_user_settings(
    config_dir: Path, *, managed_dirs: Sequence[Path] | None = None
) -> tuple[list[str], str]:
    """user·managed 설정을 읽기 전용으로 검사한다. (위험 사유 목록, 내용 해시).

    읽을 수 없거나(권한·인코딩) JSON이 깨졌으면 `unreadable:<파일>` 사유를 낸다(fail-closed).
    `~/.claude/CLAUDE.md`(user 메모리)는 해시 대상에만 넣는다(사후 변경 감지, M-1·M-2).
    `managed_dirs`가 None이면 플랫폼 기본 위치(`default_managed_dirs`)를 본다.
    """
    findings: list[str] = []
    digest = hashlib.sha256()
    for name in _SETTINGS_FILES:
        _inspect_settings_file(digest, config_dir / name, name, findings)
    try:
        _hash_file(digest, config_dir / _USER_MEMORY_FILE, "user:" + _USER_MEMORY_FILE)
    except OSError:
        findings.append(f"unreadable:{_USER_MEMORY_FILE}")
    dirs = default_managed_dirs() if managed_dirs is None else tuple(managed_dirs)
    for index, directory in enumerate(dirs):
        prefix = f"managed{index}:"
        _inspect_settings_file(
            digest, directory / _MANAGED_SETTINGS_FILE, prefix + _MANAGED_SETTINGS_FILE, findings
        )
        try:
            raw = _hash_file(digest, directory / _MANAGED_MCP_FILE, prefix + _MANAGED_MCP_FILE)
        except OSError:
            findings.append(f"unreadable:{prefix}{_MANAGED_MCP_FILE}")
            continue
        if raw is not None:
            findings.append(f"managed_mcp:{prefix}{_MANAGED_MCP_FILE}")
    return findings, digest.hexdigest()


def ancestors_clean(work_dir: Path, *, home: Path | None = None) -> bool:
    """work 디렉터리 상위 경로에 claude 프로젝트 설정 표식이 없는지(홈의 .claude는 제외)."""
    home_dir = (home or Path.home()).resolve()
    for ancestor in work_dir.resolve().parents:
        for marker in _ANCESTOR_MARKERS:
            if ancestor == home_dir and marker == ".claude":
                continue
            if (ancestor / marker).exists():
                return False
    return True


def run_pre_checks(
    *,
    job_id: int,
    attempt: int,
    job_dir: JobDir,
    now: datetime,
    config_dir: Path | None = None,
    home: Path | None = None,
    env_source: Mapping[str, str] | None = None,
    managed_dirs: Sequence[Path] | None = None,
) -> PreCheck:
    """사전 점검. `env_source`는 러너가 `build_child_env`에 넘길 것과 **같은 Mapping**이어야 한다.

    `config_dir`는 테스트용 덮어쓰기다. 주지 않으면 `claude_config_dir(env_source)`(자식이 실제로
    읽는 경로)를 점검한다.
    """
    source = env_source if env_source is not None else os.environ
    target = config_dir if config_dir is not None else claude_config_dir(source)
    dirs = default_managed_dirs() if managed_dirs is None else tuple(managed_dirs)
    findings, settings_hash = inspect_user_settings(target, managed_dirs=dirs)
    if config_dir_override(source) is not None:
        # 자식에게는 넘기지 않으므로 점검 경로와 실제 경로가 어긋날 수 있다(H-1) — 미지원 구성.
        findings.insert(0, f"unsupported_env:{CONFIG_DIR_ENV}")
    if child_home_dir(source) is None:
        # 자식에게 넘길 홈 키(Windows=USERPROFILE, 그 밖=HOME)가 없으면 점검은 Path.home()
        # (HOMEDRIVE+HOMEPATH 등)으로, 자식 Node는 OS 프로필 API로 홈을 찾아 둘이 어긋날 수 있다
        # (Spinoza 34번 N-1) — 미지원 구성으로 보고 fallback3.
        findings.insert(0, UNSUPPORTED_HOME_MISSING)
    if R1_SETTING_SOURCES_VERIFIED:
        mode: IsolationMode = "setting_sources"
    else:
        mode = "fallback3" if findings else "user_settings_clean"
    return PreCheck(
        job_id=job_id,
        attempt=attempt,
        pre_checked_at=now.isoformat(),
        jobdir_fresh=job_dir.fresh,
        jobdir_acl=job_dir.acl,
        ancestor_clean=ancestors_clean(job_dir.work, home=home),
        isolation_mode=mode,
        isolation_findings=tuple(findings),
        user_settings_sha256=settings_hash,
        config_dir=target,
        managed_dirs=dirs,
    )


def _unexpected_files(job_dir: JobDir) -> tuple[str, ...]:
    found: list[str] = []
    try:
        for entry in job_dir.work.iterdir():
            found.append(f"work/{entry.name}")
        for path in job_dir.root.rglob("*"):
            name = path.name.lower()
            if name in _UNEXPECTED_NAMES or name.startswith("settings"):
                rel = path.relative_to(job_dir.root).as_posix()
                if rel not in found:
                    found.append(rel)
    except OSError:
        found.append("<unreadable>")
    return tuple(sorted(found)[:20])


def finalize(
    pre: PreCheck,
    *,
    job_dir: JobDir,
    now: datetime,
    run: RunFacts,
    verified_body_sha256: str | None,
) -> PreflightResult:
    """실행 후 점검을 마치고 PreflightResult(claude 실행 잡)를 만든다.

    설정 해시는 사전점검과 같은 대상(`pre.config_dir`, `pre.managed_dirs`)을 다시 계산한다.
    """
    _findings, settings_hash = inspect_user_settings(pre.config_dir, managed_dirs=pre.managed_dirs)
    return PreflightResult(
        job_id=pre.job_id,
        attempt=pre.attempt,
        pre_checked_at=pre.pre_checked_at,
        post_checked_at=now.isoformat(),
        claude_executed=True,
        jobdir_fresh=pre.jobdir_fresh,
        jobdir_acl="owner_only" if pre.jobdir_acl == "owner_only" else "failed",
        ancestor_clean=pre.ancestor_clean,
        isolation_mode=pre.isolation_mode,
        user_settings_sha256=pre.user_settings_sha256,
        user_settings_unchanged=settings_hash == pre.user_settings_sha256,
        unexpected_files=_unexpected_files(job_dir),
        tools_used=run.tools_used,
        submit_count=run.submit_count,
        turns=run.turns,
        exit_code=run.exit_code,
        verified_body_sha256=verified_body_sha256,
    )


def for_fixed_template(*, job_id: int, attempt: int, now: datetime) -> PreflightResult:
    """고정 템플릿 잡(claude 미실행). 잡 디렉터리·토큰·G6를 건너뛴다(§7.9)."""
    stamp = now.isoformat()
    return PreflightResult(
        job_id=job_id,
        attempt=attempt,
        pre_checked_at=stamp,
        post_checked_at=stamp,
        claude_executed=False,
        jobdir_fresh=True,
        jobdir_acl="owner_only",
        ancestor_clean=True,
        isolation_mode="not_applicable",
        user_settings_sha256="",
        user_settings_unchanged=True,
        unexpected_files=(),
        tools_used=(),
        submit_count=1,
        turns=0,
        exit_code=None,
        verified_body_sha256=None,
    )
