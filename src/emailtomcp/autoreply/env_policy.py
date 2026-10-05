"""`claude -p` 자식 프로세스 환경변수·도구 정책 (DESIGN.md §2(c) C-4·C-8) — P3 Phase B.

- 환경변수는 **allowlist로만** 넘긴다. `ANTHROPIC_*`, `CLAUDE_CODE_*`, `NODE_OPTIONS`,
  `NODE_PATH`는 allowlist에 없을 뿐 아니라 마지막에 한 번 더 지운다(설정으로 끌 수 없다).
- `TEMP`/`TMP`/`TMPDIR`은 잡 전용 디렉터리로 바꾼다.
- PATH는 고정한 claude(또는 node) 디렉터리와 시스템 디렉터리만 남긴다.
- 프록시(`HTTPS_PROXY` 등)는 "앱 설정에서 사용자가 명시적으로 지정한 값"만 넘긴다 — 이번
  단계에는 그 설정 경로가 없어 넘기지 않는다(U2).
- 허용 도구는 잡 종류별 상수다. Phase B는 kind=auto_reply·draft 계획만 있고, 스레드 도구
  (`get_job_thread`)는 아직 서버에 없어 넣지 않는다.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from emailtomcp.core.autoreply_types import AUTOREPLY_DRAFT_ALLOWED_TOOLS

MCP_SERVER_KEY = "emailtomcp"
TOOL_PREFIX = f"mcp__{MCP_SERVER_KEY}__"
TOOL_GET_JOB_MESSAGE = "get_job_message"
TOOL_SUBMIT_AUTO_REPLY = "submit_auto_reply"

# --allowedTools 상수(잡 종류·계획별). 서버 쪽 scopes.SCOPE_TOOLS[SCOPE_JOB]와 같은 집합이다.
# 정의는 core(G7 판정기도 같은 집합으로 재확인, Spinoza 34번 L-5).
ALLOWED_TOOLS_AUTO_REPLY_DRAFT: tuple[str, ...] = AUTOREPLY_DRAFT_ALLOWED_TOOLS

# R1-4 폴백용 내장 도구 목록(`--tools ""` 미지원 시 --disallowedTools에 나열). 지금은 쓰지 않는다.
KNOWN_BUILTIN_TOOLS: tuple[str, ...] = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "LS",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "TodoWrite",
    "Skill",
    "SlashCommand",
    "ExitPlanMode",
)

JOB_TOKEN_ENV = "EMAILTOMCP_JOB_TOKEN"

_WINDOWS_KEYS = (
    "SYSTEMROOT",
    "WINDIR",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
)
_POSIX_KEYS = ("HOME", "USER", "LOGNAME")
_ALWAYS_REMOVED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
# CLAUDE_CONFIG_DIR: 자식 claude가 읽을 user 설정 디렉터리를 바꾼다. 앱은 이 구성을 지원하지 않고
# (사전점검이 fallback3으로 취소, Spinoza 30번 H-1), 자식에게도 절대 넘기지 않는다.
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
_ALWAYS_REMOVED = ("NODE_OPTIONS", "NODE_PATH", CONFIG_DIR_ENV)


def _system_path_dirs(source: Mapping[str, str]) -> list[str]:
    if sys.platform == "win32":
        root = source.get("SYSTEMROOT") or source.get("SystemRoot") or r"C:\Windows"
        return [
            os.path.join(root, "System32"),
            root,
            os.path.join(root, "System32", "Wbem"),
        ]
    return ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def _get_ci(source: Mapping[str, str], key: str) -> str | None:
    """Windows 환경변수는 대소문자를 구분하지 않는다."""
    if key in source:
        return source[key]
    if sys.platform == "win32":
        upper = key.upper()
        for k, v in source.items():
            if k.upper() == upper:
                return v
    return None


def config_dir_override(source: Mapping[str, str]) -> str | None:
    """원본 환경에 CLAUDE_CONFIG_DIR이 (대소문자 무관) 설정돼 있으면 그 값."""
    value = _get_ci(source, CONFIG_DIR_ENV)
    return value or None


def child_home_dir(source: Mapping[str, str]) -> Path | None:
    """`build_child_env(source, ...)`로 만든 자식 환경에서 claude가 보게 될 홈 디렉터리.

    자식에게 넘기는 키(Windows=USERPROFILE, 그 밖=HOME)와 같은 키·같은 조회 규칙을 쓴다 —
    원본 환경과 자식 환경 어느 쪽을 넣어도 같은 값이 나와야 한다(사전점검 대상 = 자식 실제 경로).
    """
    key = "USERPROFILE" if sys.platform == "win32" else "HOME"
    value = _get_ci(source, key)
    return Path(value) if value else None


def build_child_env(
    source: Mapping[str, str],
    *,
    job_tmp: Path,
    token: str,
    program_dirs: Iterable[Path],
) -> dict[str, str]:
    """allowlist 환경변수만 담은 새 dict(원본은 바꾸지 않는다)."""
    env: dict[str, str] = {}
    keys = _WINDOWS_KEYS if sys.platform == "win32" else _POSIX_KEYS
    for key in keys:
        value = _get_ci(source, key)
        if value:
            env[key] = value
    for key, value in source.items():
        if key == "LANG" or key.startswith("LC_"):
            env[key] = value
    tmp = str(job_tmp)
    env["TEMP"] = tmp
    env["TMP"] = tmp
    if sys.platform != "win32":
        env["TMPDIR"] = tmp
    path_dirs: list[str] = []
    for directory in [*(str(p) for p in program_dirs), *_system_path_dirs(source)]:
        if directory and directory not in path_dirs:
            path_dirs.append(directory)
    env["PATH"] = os.pathsep.join(path_dirs)
    env[JOB_TOKEN_ENV] = token
    # 설정으로 끌 수 없는 최종 제거(allowlist에 없어도 한 번 더, C-8).
    for key in list(env):
        if key in _ALWAYS_REMOVED or key.startswith(_ALWAYS_REMOVED_PREFIXES):
            del env[key]
    return env
