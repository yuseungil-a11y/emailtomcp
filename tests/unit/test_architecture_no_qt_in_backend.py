"""아키텍처 테스트: backend 계열 패키지는 PySide6를 import하지 않는다.

갈릴레오 §0: "backend가 PySide6를 import하면 단위 테스트에 QApplication이 필요해진다"는
문제를 막기 위한 회귀 테스트다. AST만 보고 실제 import는 하지 않으므로 PySide6가 설치되어
있지 않은 환경에서도 이 테스트는 동작한다.

DESIGN.md §4.2 의존 규칙상 PySide6를 import하면 안 되는 패키지는 `ui`(UI 전용, PySide6를
직접 쓴다)와 `runtime`(qt_bridge.py만 예외적으로 PySide6를 쓴다) 두 패키지를 제외한 나머지
전부다. 패키지 목록을 하드코딩하지 않고 `src/emailtomcp` 하위를 글롭으로 훑어, 새 패키지
(mail/rules/autoreply/mcp_server/update 등)가 추가돼도 자동으로 검사 대상에 들어가게 한다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "emailtomcp"

# PySide6를 알아도 되는 예외 패키지: ui(화면 전용), runtime(qt_bridge.py만 예외).
_ALLOWED_PYSIDE6_PACKAGES = {"ui", "runtime"}


def _discover_backend_packages() -> tuple[str, ...]:
    return tuple(
        sorted(
            p.name
            for p in SRC_ROOT.iterdir()
            if p.is_dir()
            and p.name != "__pycache__"
            and p.name not in _ALLOWED_PYSIDE6_PACKAGES
            and (p / "__init__.py").exists()
        )
    )


FORBIDDEN_PACKAGES = _discover_backend_packages()
FORBIDDEN_SINGLE_FILES = (SRC_ROOT / "runtime" / "backend.py",)


def _imports_pyside6(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.startswith("PySide6") for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("PySide6"):
            return True
    return False


def _iter_python_files(package_dir: Path):
    yield from package_dir.rglob("*.py")


@pytest.mark.parametrize("package_name", FORBIDDEN_PACKAGES)
def test_package_does_not_import_pyside6(package_name: str) -> None:
    package_dir = SRC_ROOT / package_name
    offending = [p for p in _iter_python_files(package_dir) if _imports_pyside6(p)]
    assert not offending, f"{package_name} 패키지가 PySide6를 import함: {offending}"


def test_backend_module_does_not_import_pyside6() -> None:
    for path in FORBIDDEN_SINGLE_FILES:
        assert not _imports_pyside6(path), f"{path}가 PySide6를 import함"
