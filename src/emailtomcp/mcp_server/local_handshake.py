"""stdio 프록시 ↔ 앱 로컬 핸드셰이크 (DESIGN.md §6.4 2번, §2(a) L2, M2).

전송로는 앱의 단일 인스턴스용 `QLocalServer`(사용자 전용 `UserAccessOption`)다.
Windows에서는 이름 있는 파이프(`\\\\.\\pipe\\<이름>`), 그 밖의 OS에서는 임시 디렉터리의
유닉스 도메인 소켓이다. 이 모듈은 Qt를 import하지 않는다 — 앱 쪽 Qt 코드(app.py)는
`parse_command`/`build_response`만 쓰고, 프록시 쪽은 표준 라이브러리로 직접 접속한다.

허용 명령은 두 가지뿐이다(L2).
- `activate` — 두 번째 실행이 기존 창을 활성화할 때 보낸다.
- `mcp_handshake <nonce 32자 hex>` — 프록시가 보낸다.

핸드셰이크
1. 프록시가 랜덤 nonce(16바이트)를 보낸다.
2. 앱은 `HMAC-SHA256(keyring 공유비밀, 도메인구분자|nonce|status|port)`와 현재 포트를
   JSON 한 줄로 돌려준다. MCP가 꺼져 있으면(포트 충돌 등) `status="mcp_disabled"`.
3. 프록시는 HMAC을 `compare_digest`로 검증한다. 다르면 `SecurityError`로 즉시 중단한다
   — 공유비밀을 모르는 가짜 서버(포트 선점·사칭)에는 토큰을 보내지 않는다.
   포트도 HMAC에 묶여 있으므로 응답의 포트만 바꿔치기할 수도 없다.

공유비밀 자체는 전송하지 않는다(§8.3).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time

from emailtomcp.core.errors import SecurityError

SINGLE_INSTANCE_KEY = "EmailToMCP-single-instance"

CMD_ACTIVATE = "activate"
CMD_HANDSHAKE = "mcp_handshake"
CMD_INVALID = "invalid"

STATUS_OK = "ok"
STATUS_MCP_DISABLED = "mcp_disabled"

MAX_COMMAND_BYTES = 128
MAX_RESPONSE_BYTES = 4096
_DOMAIN = b"emailtomcp-handshake-v1"
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


class AppNotRunningError(RuntimeError):
    """앱(로컬 서버)에 접속할 수 없다 — 앱이 실행 중이 아니다."""


class McpDisabledError(RuntimeError):
    """앱은 실행 중이지만 MCP 서버가 꺼져 있다(포트 충돌 등)."""


# ---------------------------------------------------------------- 공통


def _mac(secret: bytes, nonce_hex: str, status: str, port: int) -> str:
    message = b"|".join(
        [_DOMAIN, nonce_hex.encode("ascii"), status.encode("ascii"), str(port).encode("ascii")]
    )
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def new_nonce_hex() -> str:
    return secrets.token_hex(16)


def build_request(nonce_hex: str) -> bytes:
    return f"{CMD_HANDSHAKE} {nonce_hex}\n".encode("ascii")


# ---------------------------------------------------------------- 앱(서버) 쪽


def parse_command(data: bytes) -> tuple[str, str | None]:
    """로컬 서버가 받은 바이트열을 명령으로 해석한다. 허용 외 입력은 `CMD_INVALID`."""
    if len(data) > MAX_COMMAND_BYTES:
        return CMD_INVALID, None
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return CMD_INVALID, None
    if text == CMD_ACTIVATE:
        return CMD_ACTIVATE, None
    parts = text.split(" ")
    if len(parts) == 2 and parts[0] == CMD_HANDSHAKE and _NONCE_RE.match(parts[1]):
        return CMD_HANDSHAKE, parts[1]
    return CMD_INVALID, None


def build_response(secret: bytes, nonce_hex: str, port: int | None) -> bytes:
    """핸드셰이크 응답(JSON 한 줄). `port`가 None이면 MCP 비활성 응답이다."""
    status = STATUS_OK if port else STATUS_MCP_DISABLED
    port_value = int(port or 0)
    payload = {
        "v": 1,
        "status": status,
        "port": port_value,
        "mac": _mac(secret, nonce_hex, status, port_value),
    }
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("ascii")


# ---------------------------------------------------------------- 프록시(클라이언트) 쪽


def verify_response(secret: bytes, nonce_hex: str, data: bytes) -> int:
    """응답을 검증하고 포트를 돌려준다. HMAC 불일치·형식 오류는 `SecurityError`."""
    try:
        payload = json.loads(data.decode("ascii").strip())
        status = str(payload["status"])
        port = int(payload["port"])
        mac = str(payload["mac"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise SecurityError("로컬 서버 응답 형식이 올바르지 않습니다(가짜 서버 가능성)") from exc
    expected = _mac(secret, nonce_hex, status, port)
    if not hmac.compare_digest(expected, mac):
        raise SecurityError("로컬 서버 인증(HMAC)에 실패했습니다(가짜 서버 가능성)")
    if status == STATUS_MCP_DISABLED:
        raise McpDisabledError(
            "EmailToMCP 앱의 MCP 서버가 꺼져 있습니다(포트 충돌 등). 앱을 확인하세요."
        )
    if status != STATUS_OK or not (1 <= port <= 65535):
        raise SecurityError("로컬 서버 응답 값이 올바르지 않습니다")
    return port


def local_server_path(server_name: str = SINGLE_INSTANCE_KEY) -> str:
    """QLocalServer.listen(name)이 실제로 쓰는 경로(Windows 파이프 / 유닉스 소켓)."""
    if sys.platform == "win32":
        return rf"\\.\pipe\{server_name}"
    return os.path.join(tempfile.gettempdir(), server_name)


def _exchange_windows(path: str, request: bytes, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    handle = None
    while handle is None:
        try:
            handle = open(path, "r+b", buffering=0)  # noqa: SIM115 — 아래에서 명시적으로 닫는다
        except FileNotFoundError as exc:
            raise AppNotRunningError(
                "EmailToMCP 앱이 실행 중이 아닙니다. 앱을 먼저 실행하세요."
            ) from exc
        except OSError as exc:
            # 파이프가 바쁨(ERROR_PIPE_BUSY) — 잠깐 기다렸다 다시 시도한다.
            if time.monotonic() > deadline:
                raise AppNotRunningError(f"EmailToMCP 앱에 접속하지 못했습니다: {exc}") from exc
            time.sleep(0.05)

    result: dict[str, bytes | BaseException] = {}

    def _io() -> None:
        try:
            handle.write(request)
            buf = b""
            while b"\n" not in buf and len(buf) < MAX_RESPONSE_BYTES:
                chunk = handle.read(512)
                if not chunk:
                    break
                buf += chunk
            result["data"] = buf
        except BaseException as exc:  # noqa: BLE001 — 스레드 밖으로 전달
            result["error"] = exc

    worker = threading.Thread(target=_io, daemon=True)
    worker.start()
    worker.join(max(0.1, deadline - time.monotonic()))
    with contextlib.suppress(OSError):
        handle.close()
    if worker.is_alive() or "data" not in result:
        raise AppNotRunningError("EmailToMCP 앱이 핸드셰이크에 응답하지 않습니다.")
    return result["data"]  # type: ignore[return-value]


def _exchange_unix(path: str, request: bytes, timeout: float) -> bytes:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(path)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise AppNotRunningError(
                "EmailToMCP 앱이 실행 중이 아닙니다. 앱을 먼저 실행하세요."
            ) from exc
        sock.sendall(request)
        buf = b""
        while b"\n" not in buf and len(buf) < MAX_RESPONSE_BYTES:
            chunk = sock.recv(512)
            if not chunk:
                break
            buf += chunk
        return buf
    except TimeoutError as exc:
        raise AppNotRunningError("EmailToMCP 앱이 핸드셰이크에 응답하지 않습니다.") from exc
    finally:
        sock.close()


def client_handshake(
    secret: bytes, *, server_name: str = SINGLE_INSTANCE_KEY, timeout: float = 3.0
) -> int:
    """앱에 접속해 핸드셰이크하고, 검증된 MCP 포트를 돌려준다(블로킹)."""
    nonce_hex = new_nonce_hex()
    path = local_server_path(server_name)
    exchange = _exchange_windows if sys.platform == "win32" else _exchange_unix
    data = exchange(path, build_request(nonce_hex), timeout)
    if not data:
        raise SecurityError("로컬 서버가 빈 응답을 보냈습니다(가짜 서버 가능성)")
    return verify_response(secret, nonce_hex, data)
