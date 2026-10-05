"""`claude -p` 명령줄 구성과 stream-json 사후 검증 (DESIGN.md §2(c) C-1~C-10) — P3 Phase B.

argv에는 **앱 내부 상수와 앱이 만든 경로만** 넣는다(C-1). 사용자·메일에서 온 문자열(규칙명,
추가 지침, 계정명)은 stdin 프롬프트로만 간다(C-2). `--bare`는 쓰지 않는다(C-6, 구독 로그인).

```
<고정된 claude.exe>  (또는 <node> <cli.js>)
  -p --output-format stream-json --verbose --max-turns 6
  --mcp-config <job>/cfg/mcp-config.json --strict-mcp-config
  --setting-sources project --tools "" --allowedTools <상수>
  --permission-mode dontAsk --append-system-prompt <SYSTEM_PROMPT 상수>
```

stream-json(R1-3 실측 전 가정 스키마): 줄마다 JSON 하나. `type=assistant`의
`message.content[]` 중 `type=tool_use`의 `name`이 도구 호출, `type=result`의 `num_turns`·
`is_error`가 최종 결과다. 알 수 없는 줄은 무시하되, result 줄이 없으면 실패로 본다(fail-closed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from emailtomcp.autoreply.cli_locator import PinnedCli
from emailtomcp.autoreply.env_policy import TOOL_PREFIX, TOOL_SUBMIT_AUTO_REPLY
from emailtomcp.autoreply.prompts import SYSTEM_PROMPT

MAX_TURNS = 6
DEFAULT_TIMEOUT_SEC = 120
MIN_TIMEOUT_SEC = 30
MAX_TIMEOUT_SEC = 600


def build_argv(
    pin: PinnedCli, *, mcp_config: Path, allowed_tools: tuple[str, ...], max_turns: int = MAX_TURNS
) -> list[str]:
    return [
        *pin.argv_prefix(),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(int(max_turns)),
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--tools",
        "",
        "--allowedTools",
        ",".join(allowed_tools),
        "--permission-mode",
        "dontAsk",
        "--append-system-prompt",
        SYSTEM_PROMPT,
    ]


@dataclass(frozen=True, slots=True)
class StreamReport:
    tools_used: tuple[str, ...]
    submit_count: int
    turns: int | None
    is_error: bool
    result_seen: bool
    unparsed_lines: int


def parse_stream(data: bytes) -> StreamReport:
    tools: list[str] = []
    turns: int | None = None
    is_error = False
    result_seen = False
    unparsed = 0
    for raw_line in data.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            unparsed += 1
            continue
        if not isinstance(event, dict):
            unparsed += 1
            continue
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        tools.append(str(item.get("name", "")))
        elif kind == "result":
            result_seen = True
            value = event.get("num_turns")
            if isinstance(value, int) and not isinstance(value, bool):
                turns = value
            is_error = bool(event.get("is_error")) or event.get("subtype") not in (None, "success")
    submit_name = TOOL_PREFIX + TOOL_SUBMIT_AUTO_REPLY
    return StreamReport(
        tools_used=tuple(tools),
        submit_count=sum(1 for t in tools if t == submit_name),
        turns=turns,
        is_error=is_error,
        result_seen=result_seen,
        unparsed_lines=unparsed,
    )
