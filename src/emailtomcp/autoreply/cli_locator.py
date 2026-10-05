"""claude CLI 경로 탐지와 고정 (DESIGN.md §9.3, §2(c) C-3) — P3 Phase B.

- 후보 수집은 **실행하지 않고 경로만** 모은다(`discover_candidates`).
- 사용자가 화면(Claude/MCP 패널 > 잡 탭 > [claude CLI 찾기])에서 확인한 경로만 **절대경로 + sha256**
  으로 고정한다(`make_pin` → 설정 `claude.cli_pin`). 실행 직전마다 해시를 다시 대조하고, 바뀌었으면
  실행하지 않는다(재확인 필요, §9.3 6번).
- cmd.exe를 거치지 않는다(C-3): `.cmd`/`.bat`/`.ps1`은 고정할 수 없다. npm 설치본(`claude.cmd`)은
  같은 폴더의 `node_modules/@anthropic-ai/claude-code/cli.js`와 `node(.exe)`로 풀어서
  `node <cli.js>`로 직접 실행한다. 풀 수 없으면 후보에서 뺀다.
- 임시·다운로드 폴더와 현재 작업 폴더 아래 경로는 거부한다.
- `--version`·`auth status` 실행(§11.5 [테스트])은 이번 단계에서 하지 않는다.

`claude.` 접두사 설정은 범용 `UiApi.set_setting`으로 쓸 수 없다(app.py 보호 접두사).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SETTING_CLI_PIN = "claude.cli_pin"
_FORBIDDEN_SUFFIXES = (".cmd", ".bat", ".ps1", ".vbs", ".js")  # .js는 script 자리에만 허용
_NPM_CLI_REL = Path("node_modules") / "@anthropic-ai" / "claude-code" / "cli.js"


@dataclass(frozen=True, slots=True)
class PinnedCli:
    program: Path
    script: Path | None
    program_sha256: str
    script_sha256: str | None

    def argv_prefix(self) -> list[str]:
        return [str(self.program)] + ([str(self.script)] if self.script else [])

    def program_dirs(self) -> list[Path]:
        return [self.program.parent]


@dataclass(frozen=True, slots=True)
class CliCandidate:
    program: str
    script: str | None
    source: str  # "native" | "path" | "npm"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unsafe_roots() -> list[Path]:
    roots = [Path(tempfile.gettempdir()), Path.cwd(), Path.home() / "Downloads"]
    result: list[Path] = []
    for root in roots:
        try:
            result.append(root.resolve())
        except OSError:
            continue
    return result


def is_unsafe_location(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return True
    return any(resolved == root or root in resolved.parents for root in _unsafe_roots())


def _resolve_npm_shim(shim: Path) -> CliCandidate | None:
    script = shim.parent / _NPM_CLI_REL
    if not script.is_file():
        return None
    node_name = "node.exe" if sys.platform == "win32" else "node"
    node = shim.parent / node_name
    if not node.is_file():
        found = shutil.which("node")
        if not found:
            return None
        node = Path(found)
    return CliCandidate(program=str(node), script=str(script), source="npm")


def discover_candidates() -> list[CliCandidate]:
    """claude 후보 경로 목록(실행하지 않는다). 안전하지 않은 위치는 뺀다."""
    names = ("claude.exe", "claude") if sys.platform == "win32" else ("claude",)
    paths: list[CliCandidate] = []
    native_dirs = [Path.home() / ".local" / "bin", Path.home() / ".claude" / "local"]
    for directory in native_dirs:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                paths.append(CliCandidate(program=str(candidate), script=None, source="native"))
    found = shutil.which("claude")
    if found:
        path = Path(found)
        if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
            resolved = _resolve_npm_shim(path)
            if resolved is not None:
                paths.append(resolved)
        else:
            paths.append(CliCandidate(program=str(path), script=None, source="path"))
    appdata = os.environ.get("APPDATA")
    if sys.platform == "win32" and appdata:
        shim = Path(appdata) / "npm" / "claude.cmd"
        if shim.is_file():
            resolved = _resolve_npm_shim(shim)
            if resolved is not None:
                paths.append(resolved)
    unique: list[CliCandidate] = []
    seen: set[tuple[str, str | None]] = set()
    for item in paths:
        key = (str(Path(item.program).resolve()), item.script)
        if key in seen:
            continue
        seen.add(key)
        if is_unsafe_location(Path(item.program)) or (
            item.script and is_unsafe_location(Path(item.script))
        ):
            continue
        unique.append(item)
    return unique


def make_pin(program: str, script: str | None, *, now: datetime | None = None) -> dict[str, Any]:
    """사용자가 확인한 경로를 고정값(dict, 설정에 저장)으로 만든다. 문제가 있으면 ValueError."""
    prog = Path(program)
    if not prog.is_absolute() or not prog.is_file():
        raise ValueError("claude 실행 파일 경로가 올바르지 않습니다(절대경로의 파일이어야 합니다)")
    if prog.suffix.lower() in _FORBIDDEN_SUFFIXES:
        raise ValueError(
            "cmd/bat/ps1 같은 스크립트는 고정할 수 없습니다(C-3). 네이티브 설치를 쓰세요"
        )
    if is_unsafe_location(prog):
        raise ValueError("임시·다운로드·현재 작업 폴더 아래의 실행 파일은 고정할 수 없습니다")
    script_path: Path | None = None
    if script:
        script_path = Path(script)
        if not script_path.is_absolute() or not script_path.is_file():
            raise ValueError("cli.js 경로가 올바르지 않습니다")
        if script_path.suffix.lower() != ".js" or is_unsafe_location(script_path):
            raise ValueError("cli.js 경로가 올바르지 않습니다")
    return {
        "program": str(prog.resolve()),
        "script": str(script_path.resolve()) if script_path else None,
        "program_sha256": file_sha256(prog),
        "script_sha256": file_sha256(script_path) if script_path else None,
        "pinned_at": (now or datetime.now(UTC)).isoformat(),
    }


def pin_from_setting(value: Any) -> PinnedCli | None:
    if not isinstance(value, dict):
        return None
    program = value.get("program")
    program_sha = value.get("program_sha256")
    if not isinstance(program, str) or not isinstance(program_sha, str):
        return None
    script = value.get("script")
    script_sha = value.get("script_sha256")
    if script is not None and (not isinstance(script, str) or not isinstance(script_sha, str)):
        return None
    return PinnedCli(
        program=Path(program),
        script=Path(script) if script else None,
        program_sha256=program_sha,
        script_sha256=script_sha if script else None,
    )


def verify_pin(pin: PinnedCli | None) -> str | None:
    """실행 직전 확인. 문제가 있으면 사유(한국어), 없으면 None."""
    if pin is None:
        return "claude CLI가 고정되지 않았습니다(Claude/MCP 패널 > 잡 탭에서 고정하세요)"
    if pin.program.suffix.lower() in _FORBIDDEN_SUFFIXES:
        return "고정된 claude 경로가 스크립트 형식입니다(C-3)"
    try:
        if not pin.program.is_file() or file_sha256(pin.program) != pin.program_sha256:
            return "claude 실행 파일이 바뀌었거나 없습니다 — 다시 고정해야 합니다"
        if pin.script is not None and (
            not pin.script.is_file() or file_sha256(pin.script) != pin.script_sha256
        ):
            return "claude cli.js가 바뀌었거나 없습니다 — 다시 고정해야 합니다"
    except OSError:
        return "claude 실행 파일을 읽을 수 없습니다"
    return None
