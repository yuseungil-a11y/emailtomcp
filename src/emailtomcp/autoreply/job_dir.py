"""잡 전용 디렉터리 (DESIGN.md §2(c) C-9, §7.11 A8 `jobdir_fresh`·`jobdir_acl`) — P3 Phase B.

구조: `<user_data>/jobs/<job_id>-<attempt>-<랜덤>/{work,cfg,tmp}`
- `work/`: claude의 cwd. 비어 있는 디렉터리다(프로젝트 설정 자동 로드 회피).
- `cfg/mcp-config.json`: 형제 디렉터리(C-9). 토큰 값은 넣지 않고 `${EMAILTOMCP_JOB_TOKEN}` 참조만
  쓴다(H4). 파일은 처음부터 사용자 전용으로 만든다(POSIX `O_CREAT|O_EXCL, 0o600`).
- 권한: POSIX는 0700. Windows는 **상속을 끊고 현재 사용자 SID만 모든 권한을 갖는 DACL**을 붙인다
  (`D:P(A;OICI;FA;;;<SID>)`, ctypes로 advapi32 호출 — 외부 프로세스 실행 없음). 실패하면
  `acl="failed"`로 보고하고 계속한다(§7.11 fail-closed: 결과는 초안 — Phase B는 원래 초안뿐).
- 잡이 끝나면 통째로 지운다. 삭제 실패는 호출자가 autosend_state=paused_security로 처리한다.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MCP_CONFIG_NAME = "mcp-config.json"


@dataclass(frozen=True, slots=True)
class JobDir:
    root: Path
    work: Path
    cfg: Path
    tmp: Path
    fresh: bool
    acl: str  # "owner_only" | "failed"


def _apply_owner_only_dacl_windows(path: Path) -> bool:
    """현재 프로세스 사용자 SID만 허용하는 보호 DACL(상속 차단)을 붙인다."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL

    token_query = 0x0008
    token_user = 1
    sddl_revision_1 = 1
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000

    token = wintypes.HANDLE()
    process = kernel32.GetCurrentProcess()
    if not advapi32.OpenProcessToken(process, token_query, ctypes.byref(token)):
        return False
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(size))
        if size.value == 0:
            return False
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, token_user, buffer, size, ctypes.byref(size)):
            return False
        # TOKEN_USER { SID_AND_ATTRIBUTES User { PSID Sid; DWORD Attributes } } — 첫 필드가 PSID.
        psid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(sid_text)):
            return False
        try:
            sid = sid_text.value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
    finally:
        kernel32.CloseHandle(token)
    if not sid or not sid.startswith("S-1-"):
        return False

    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;OICI;FA;;;{sid})", sddl_revision_1, ctypes.byref(descriptor), None
    ):
        return False
    try:
        return bool(
            advapi32.SetFileSecurityW(
                str(path),
                dacl_security_information | protected_dacl_security_information,
                descriptor,
            )
        )
    finally:
        kernel32.LocalFree(ctypes.cast(descriptor, wintypes.HLOCAL))


def restrict_to_owner(path: Path) -> bool:
    """디렉터리를 현재 사용자 전용으로 만든다. 성공하면 True."""
    try:
        if sys.platform == "win32":
            return _apply_owner_only_dacl_windows(path)
        os.chmod(path, 0o700)
        return True
    except Exception:  # noqa: BLE001 — 실패는 acl="failed"로 보고(fail-closed는 호출자)
        logger.warning("잡 디렉터리 권한 설정 실패: %s", path, exc_info=True)
        return False


def create_job_dir(base: Path, *, job_id: int, attempt: int) -> JobDir:
    """새 잡 디렉터리를 만든다. 이미 있으면 OSError(재사용하지 않는다 — jobdir_fresh)."""
    base.mkdir(parents=True, exist_ok=True)
    root = base / f"{int(job_id)}-{int(attempt)}-{secrets.token_hex(4)}"
    root.mkdir(mode=0o700, exist_ok=False)
    fresh = not any(root.iterdir())
    acl_ok = restrict_to_owner(root)
    work = root / "work"
    cfg = root / "cfg"
    tmp = root / "tmp"
    # Windows에서 Python 3.13+의 mkdir(mode=0o700)은 자체 DACL(SYSTEM·Administrators 포함)을
    # 붙여 루트의 보호 DACL 상속을 끊는다 — 하위 디렉터리는 기본 모드로 만들어 상속받게 한다.
    sub_mode = 0o777 if sys.platform == "win32" else 0o700
    for sub in (work, cfg, tmp):
        sub.mkdir(mode=sub_mode, exist_ok=False)
    return JobDir(
        root=root,
        work=work,
        cfg=cfg,
        tmp=tmp,
        fresh=fresh,
        acl="owner_only" if acl_ok else "failed",
    )


def write_mcp_config(job_dir: JobDir, *, port: int, token_env: str) -> Path:
    """`cfg/mcp-config.json`을 만든다. 토큰 값은 쓰지 않고 환경변수 참조만 쓴다(H4)."""
    config = {
        "mcpServers": {
            "emailtomcp": {
                "type": "http",
                "url": f"http://127.0.0.1:{int(port)}/mcp",
                "headers": {"Authorization": "Bearer ${" + token_env + "}"},
            }
        }
    }
    path = job_dir.cfg / MCP_CONFIG_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(json.dumps(config).encode("utf-8"))
    return path


def remove_job_dir(job_dir: JobDir | Path) -> bool:
    root = job_dir.root if isinstance(job_dir, JobDir) else job_dir
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning("잡 디렉터리 삭제 실패: %s", root, exc_info=True)
        return False
    return not root.exists()


def cleanup_stale_job_dirs(base: Path) -> int:
    """기동 시 남은 잡 디렉터리 정리(§5.6). 지운 개수."""
    if not base.is_dir():
        return 0
    removed = 0
    for child in base.iterdir():
        if child.is_dir() and remove_job_dir(child):
            removed += 1
    return removed
