"""첨부 안전 처리 테스트 (DESIGN.md §8.5 H6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from emailtomcp.mail.attachments_safety import (
    PathTraversalError,
    is_dangerous_extension,
    resolve_safe_path,
    sanitize_filename,
)


def test_sanitize_filename_removes_forbidden_chars() -> None:
    # "/"와 "\"는 경로 구분자로 취급되어 별도 로직(마지막 세그먼트만 남김)으로 처리되므로
    # 여기서는 그 외 금지문자만 확인한다.
    assert sanitize_filename('a<b>c:d"e|f?g*h.txt') == "a_b_c_d_e_f_g_h.txt"


def test_sanitize_filename_strips_control_and_bidi() -> None:
    # U+202E는 RTLO(우→좌 재정렬) bidi 제어문자다.
    name = "‮exe.txt가장"
    sanitized = sanitize_filename(name)
    assert "‮" not in sanitized


def test_sanitize_filename_handles_reserved_names() -> None:
    assert sanitize_filename("CON.txt") == "_CON.txt"
    assert sanitize_filename("con").startswith("_")


def test_sanitize_filename_strips_trailing_dot_and_space() -> None:
    assert sanitize_filename("evil.txt. ") == "evil.txt"


def test_sanitize_filename_takes_last_path_segment() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename(r"..\..\windows\system32\config") == "config"


def test_sanitize_filename_limits_to_255_bytes() -> None:
    long_name = ("가" * 200) + ".txt"
    sanitized = sanitize_filename(long_name)
    assert len(sanitized.encode("utf-8")) <= 255
    assert sanitized.endswith(".txt")


def test_sanitize_filename_empty_falls_back() -> None:
    assert sanitize_filename("") == "attachment"
    assert sanitize_filename(None) == "attachment"


@pytest.mark.parametrize(
    "name",
    ["malware.exe", "script.js", "macro.docm", "installer.msi", "shortcut.lnk", "app.scr"],
)
def test_dangerous_extensions_detected(name: str) -> None:
    assert is_dangerous_extension(name) is True


@pytest.mark.parametrize("name", ["report.pdf", "image.png", "data.csv", "노트.txt"])
def test_safe_extensions_not_flagged(name: str) -> None:
    assert is_dangerous_extension(name) is False


def test_resolve_safe_path_stays_inside_target_dir(tmp_path: Path) -> None:
    target = tmp_path / "downloads"
    target.mkdir()
    resolved = resolve_safe_path(target, "문서.pdf")
    assert resolved.parent == target.resolve()


def test_resolve_safe_path_blocks_traversal_components(tmp_path: Path) -> None:
    target = tmp_path / "downloads"
    target.mkdir()
    # sanitize_filename이 경로 구분자를 먼저 제거하므로 결과는 항상 target 내부에 남는다.
    resolved = resolve_safe_path(target, "../../etc/passwd")
    assert target.resolve() in resolved.parents or resolved.parent == target.resolve()


def test_resolve_safe_path_raises_when_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "downloads"
    target.mkdir()

    # sanitize_filename을 무력화해 강제로 탈출 경로를 만들어 PathTraversalError를 검증한다.
    import emailtomcp.mail.attachments_safety as mod

    monkeypatch.setattr(mod, "sanitize_filename", lambda raw: "../escaped.txt")
    with pytest.raises(PathTraversalError):
        resolve_safe_path(target, "whatever.txt")
