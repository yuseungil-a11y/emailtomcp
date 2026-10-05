"""아키텍처 테스트: 자동회신 토글 보호 키는 정해진 곳에서만 쓴다 (DESIGN.md §7.0 I6, §7.11).

- `autoreply.enabled`/`generation`/`watermark_message_id`/`enabled_changed_at`/`queue_state`/
  `autosend_state` 키 문자열은 `storage/repositories/autoreply.py`에만 있어야 한다(그 모듈이
  이 키를 쓰는 SQL의 유일한 위치). 예외: m0003 마이그레이션의 초기값 설정·v2 잔존 키 삭제
  (`autoreply.generation`, `autoreply.enabled`, `autoreply.queue_state`,
  `autoreply.autosend_state`).
- 그 모듈의 키 상수(`SETTING_ENABLED` 등)를 다른 모듈이 직접 참조하지 않는다 — app.py는
  `PROTECTED_SETTING_KEYS`(보호 목록)만 가져다 쓴다.
- `'policy_auto_send'` 리터럴은 허용된 파일에만 있어야 한다(§7.11, 데카르트 D2): `core/models.py`
  (ApprovedBy 열거형 정의), `m0001`(CHECK 제약), `storage/transitions.py`(I8 초기화),
  `storage/repositories/autoreply.py`(조회·드레인·Phase B G7). 다른 곳에서 이 값을 만들어 쓰면
  사람 승인 없는 자동발송 행을 만드는 경로가 생길 수 있다.

AST만 보고 실제 import는 하지 않는다. 모듈·클래스·함수 docstring은 설명문이므로 제외한다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "emailtomcp"

PROTECTED_KEYS = (
    "autoreply.enabled",
    "autoreply.generation",
    "autoreply.watermark_message_id",
    "autoreply.enabled_changed_at",
    "autoreply.queue_state",
    "autoreply.autosend_state",
)
KEY_CONSTANT_NAMES = {
    "SETTING_ENABLED",
    "SETTING_GENERATION",
    "SETTING_WATERMARK",
    "SETTING_ENABLED_CHANGED_AT",
    "SETTING_QUEUE_STATE",
    "SETTING_AUTOSEND_STATE",
}

OWNER = Path("storage/repositories/autoreply.py")
# 파일 → 그 파일에서 허용되는 키
EXCEPTIONS: dict[Path, set[str]] = {
    Path("storage/migrations/m0003_autoreply_toggle.py"): {
        "autoreply.generation",
        "autoreply.enabled",
        "autoreply.queue_state",
        "autoreply.autosend_state",
    },
}

POLICY_AUTO_SEND_LITERAL = "policy_auto_send"
POLICY_AUTO_SEND_ALLOWED: frozenset[Path] = frozenset(
    {
        Path("core/models.py"),
        Path("storage/migrations/m0001_init.py"),
        Path("storage/transitions.py"),
        Path("storage/repositories/autoreply.py"),
    }
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _python_files() -> list[Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> Path:
    return path.relative_to(SRC_ROOT)


def test_protected_key_literals_only_in_autoreply_repository() -> None:
    offending: list[str] = []
    for path in _python_files():
        rel = _rel(path)
        if rel == OWNER:
            continue
        allowed = EXCEPTIONS.get(rel, set())
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstrings:
                continue
            for key in PROTECTED_KEYS:
                if key in node.value and key not in allowed:
                    offending.append(f"{rel}:{node.lineno} {key!r}")
    assert not offending, "자동회신 보호 키 문자열이 허용 위치 밖에 있음:\n" + "\n".join(offending)


def test_key_constants_are_not_used_outside_autoreply_repository() -> None:
    offending: list[str] = []
    for path in _python_files():
        rel = _rel(path)
        if rel == OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.alias):
                name = node.name
            if name in KEY_CONSTANT_NAMES:
                offending.append(f"{rel}:{getattr(node, 'lineno', '?')} {name}")
    assert not offending, "자동회신 키 상수를 직접 참조함:\n" + "\n".join(offending)


def test_owner_module_defines_all_protected_keys() -> None:
    """키 문자열이 실제로 소유 모듈에 있는지(위 두 테스트가 빈 검사가 되지 않도록)."""
    text = (SRC_ROOT / OWNER).read_text(encoding="utf-8")
    for key in PROTECTED_KEYS:
        assert f'"{key}"' in text


def test_app_protected_key_list_matches_design() -> None:
    from emailtomcp.app import AUTOREPLY_PROTECTED_SETTING_KEYS

    assert set(AUTOREPLY_PROTECTED_SETTING_KEYS) == set(PROTECTED_KEYS)


def _policy_auto_send_violations(rel: Path, source: str) -> list[str]:
    """허용 파일이 아니면 docstring 밖의 문자열 상수에서 'policy_auto_send'를 찾는다.

    대소문자를 무시한다(`"POLICY_AUTO_SEND".lower()` 같은 우회는 막지 못하지만, 리터럴이 그대로
    들어가는 흔한 실수는 잡는다). 주석은 AST에 없으므로 대상이 아니다.
    """
    if rel in POLICY_AUTO_SEND_ALLOWED:
        return []
    tree = ast.parse(source, filename=str(rel))
    docstrings = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            continue
        if POLICY_AUTO_SEND_LITERAL in node.value.casefold():
            found.append(f"{rel}:{node.lineno}")
    return found


def test_policy_auto_send_literal_only_in_allowed_files() -> None:
    offending: list[str] = []
    for path in _python_files():
        offending += _policy_auto_send_violations(_rel(path), path.read_text(encoding="utf-8"))
    assert not offending, "'policy_auto_send' 리터럴이 허용 파일 밖에 있음:\n" + "\n".join(
        offending
    )


def test_policy_auto_send_allowed_files_exist() -> None:
    """허용 목록이 실제 파일을 가리키는지(경로가 바뀌어 검사가 비지 않도록)."""
    for rel in POLICY_AUTO_SEND_ALLOWED:
        assert (SRC_ROOT / rel).is_file(), rel
    # 정의 위치에는 실제로 리터럴이 있어야 한다.
    assert '"policy_auto_send"' in (SRC_ROOT / "core/models.py").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "source",
    [
        'APPROVED = "policy_auto_send"\n',
        "def f(conn):\n    conn.execute(\"UPDATE drafts SET approved_by='policy_auto_send'\")\n",
        'x = {"by": "POLICY_AUTO_SEND"}\n',
    ],
)
def test_policy_auto_send_check_catches_violation(source: str) -> None:
    """D2 회귀: 허용되지 않은 파일에 리터럴을 넣으면 검사가 실제로 잡는다."""
    assert _policy_auto_send_violations(Path("app.py"), source)
    assert _policy_auto_send_violations(Path("autoreply/runner.py"), source)


def test_policy_auto_send_check_ignores_docstrings_and_allowed_files() -> None:
    doc_only = '"""policy_auto_send 설명."""\n\ndef f():\n    """policy_auto_send."""\n'
    assert _policy_auto_send_violations(Path("app.py"), doc_only) == []
    literal = 'APPROVED = "policy_auto_send"\n'
    assert _policy_auto_send_violations(Path("storage/transitions.py"), literal) == []
