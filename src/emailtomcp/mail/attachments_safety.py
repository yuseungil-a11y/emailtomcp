"""첨부 안전 처리 (DESIGN.md §8.5 H6).

파일명 정화, 위험 확장자 판정, 저장 경로 검증(path traversal 방지), 다운로드한
파일에 대한 플랫폼별 "출처 표시"(Windows Mark-of-the-Web / macOS quarantine)를
담당한다. 표시(마킹) 실패는 저장 자체를 막지 않고 경고 로그만 남긴다.
"""

from __future__ import annotations

import logging
import platform
import re
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

# 위험 확장자(상수, §8.5) — [열기]를 막고 [저장]은 이중 확인을 받는다.
DANGEROUS_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".com",
        ".scr",
        ".pif",
        ".bat",
        ".cmd",
        ".ps1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".hta",
        ".lnk",
        ".url",
        ".msi",
        ".msix",
        ".appx",
        ".appref-ms",
        ".iso",
        ".img",
        ".vhd",
        ".vhdx",
        ".one",
        ".chm",
        ".cpl",
        ".reg",
        ".jar",
        ".docm",
        ".xlsm",
        ".pptm",
        # macOS
        ".app",
        ".command",
        ".pkg",
        ".dmg",
    }
)

# Windows 예약 장치명(확장자 없이도, 확장자가 있어도 베이스 이름이 겹치면 위험하다).
_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

_FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_FILENAME_BYTES = 255
_FALLBACK_FILENAME = "attachment"


def _strip_bidi_and_control(name: str) -> str:
    """bidi 제어문자(RTLO 포함)와 기타 제어문자를 없앤다."""
    return "".join(ch for ch in name if unicodedata.category(ch) not in ("Cf", "Cc"))


def sanitize_filename(raw: str | None) -> str:
    """첨부파일명을 안전하게 정화한다(§8.5).

    - bidi/제어문자(RTLO 포함) 제거
    - 금지문자 `<>:"/\\|?*` 제거
    - 예약명(CON, PRN, AUX, NUL, COM1~9, LPT1~9) 처리
    - ADS(`:`)를 없애고, 끝의 점/공백 제거, 255바이트 제한
    """
    name = raw or ""
    name = _strip_bidi_and_control(name)
    # 경로 구분자가 남아 있으면 마지막 세그먼트만 쓴다(디렉터리 탈출 조각 제거).
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _FORBIDDEN_CHARS_RE.sub("_", name)
    name = name.strip(" .")

    if not name:
        name = _FALLBACK_FILENAME

    stem, _, ext = name.rpartition(".")
    stem_check = (stem or name).lower()
    if stem_check in _RESERVED_NAMES:
        name = f"_{name}"

    # 255바이트(UTF-8 인코딩 기준) 제한. 확장자를 보존하려 시도한다.
    encoded = name.encode("utf-8")
    if len(encoded) > _MAX_FILENAME_BYTES:
        ext_part = f".{ext}" if ext and len(ext) <= 20 else ""
        ext_bytes = ext_part.encode("utf-8")
        budget = _MAX_FILENAME_BYTES - len(ext_bytes)
        truncated = encoded[: max(budget, 1)]
        # UTF-8 멀티바이트 중간에서 끊기지 않도록 안전하게 디코딩한다.
        base = truncated.decode("utf-8", errors="ignore")
        name = f"{base}{ext_part}"

    name = name.strip(" .")
    return name or _FALLBACK_FILENAME


def extension_of(filename: str) -> str:
    """소문자 확장자(`.exe` 형태)를 돌려준다. 없으면 빈 문자열."""
    idx = filename.rfind(".")
    if idx == -1:
        return ""
    return filename[idx:].lower()


def is_dangerous_extension(filename: str) -> bool:
    """위험 확장자(§8.5 상수 목록)인지 판정한다."""
    return extension_of(filename) in DANGEROUS_EXTENSIONS


class PathTraversalError(Exception):
    """정화된 파일명이라도 저장 경로가 대상 디렉터리를 벗어나면 발생한다."""


def resolve_safe_path(target_dir: Path, filename: str) -> Path:
    """`target_dir` 안에 들어오는지 `resolve()` 후 검증한 저장 경로를 돌려준다."""
    safe_name = sanitize_filename(filename)
    target_dir_resolved = target_dir.resolve()
    candidate = (target_dir_resolved / safe_name).resolve()
    if candidate != target_dir_resolved and target_dir_resolved not in candidate.parents:
        raise PathTraversalError(f"저장 경로가 대상 디렉터리를 벗어납니다: {candidate}")
    return candidate


def unique_path(path: Path) -> Path:
    """같은 이름의 파일이 이미 있으면 `(1)`, `(2)` 식으로 번호를 붙인다."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def mark_downloaded_file(path: Path) -> None:
    """다운로드한 파일에 플랫폼별 "출처 표시"를 남긴다.

    - Windows: `Zone.Identifier`(ADS, ZoneId=3 = 인터넷에서 받음) 작성
    - macOS: `com.apple.quarantine` 확장 속성 작성
    - 실패해도 경고 로그만 남기고 저장 자체는 막지 않는다(§8.5).
    """
    system = platform.system()
    try:
        if system == "Windows":
            _mark_windows_zone_identifier(path)
        elif system == "Darwin":
            _mark_macos_quarantine(path)
    except (OSError, AttributeError, NotImplementedError) as exc:
        logger.warning("출처 표시(MOTW/quarantine) 작성 실패(저장은 계속함): %s: %s", path, exc)


def _mark_windows_zone_identifier(path: Path) -> None:
    ads_path = Path(f"{path}:Zone.Identifier")
    ads_path.write_text("[ZoneTransfer]\r\nZoneId=3\r\n", encoding="utf-8")


def _mark_macos_quarantine(path: Path) -> None:
    """`com.apple.quarantine` 확장 속성을 작성한다.

    `os.setxattr`은 CPython에서 Linux 전용이라(macOS에는 없음) ctypes로 libc의
    `setxattr(2)`를 직접 호출한다. com.apple.quarantine 값 포맷은
    "<flags>;<timestamp(hex)>;<app-name>;<event-uuid>"다.
    """
    import ctypes
    import os

    libc = ctypes.CDLL("libc.dylib", use_errno=True)
    name = b"com.apple.quarantine"
    value = b"0083;00000000;EmailToMCP;"
    path_bytes = os.fsencode(str(path))
    ret = libc.setxattr(path_bytes, name, value, len(value), 0, 0)
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
