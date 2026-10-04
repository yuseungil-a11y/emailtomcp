from __future__ import annotations

import subprocess
import sys

import pytest
from packaging.version import InvalidVersion, Version

from emailtomcp._version import __version__


def test_version_is_single_source() -> None:
    """버전은 git 태그 기반 hatch-vcs가 만든다(DESIGN.md §15.2). 이 리포는 아직 git 메타데이터가
    없거나(dev 환경) 태그가 없을 수 있으므로, 고정 문자열이 아니라 PEP 440 유효성만 검사한다.
    git 메타데이터가 전혀 없으면 `fallback-version`인 "1.0.0.dev0+unknown"이 된다(§15.2).
    """
    try:
        Version(__version__)
    except InvalidVersion:
        pytest.fail(f"__version__이 PEP 440 형식이 아닙니다: {__version__!r}")


def test_version_is_semver_like() -> None:
    """MAJOR.MINOR.PATCH(release 부분)는 항상 세 부분이어야 한다."""
    release = Version(__version__).release
    assert len(release) == 3
    assert all(isinstance(p, int) for p in release)


@pytest.mark.integration
def test_cli_version_flag_exits_zero_before_heavy_imports() -> None:
    """--version은 Qt/DB/네트워크 초기화 전에 출력하고 종료해야 한다(갈릴레오 §0)."""
    result = subprocess.run(
        [sys.executable, "-m", "emailtomcp", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


@pytest.mark.integration
def test_cli_help_flag_exits_zero() -> None:
    # --help 설명문에 한글/특수문자(em dash 등)가 섞여 있어, Windows 콘솔 기본 코드페이지(cp949)로
    # 디코딩하면 깨질 수 있다. 인코딩을 명시해 플랫폼에 상관없이 안정적으로 비교한다.
    result = subprocess.run(
        [sys.executable, "-m", "emailtomcp", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0
    assert "emailtomcp" in result.stdout.lower()
