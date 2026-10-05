"""아키텍처 테스트: P3 Phase B 자동회신 구조 불변식 (DESIGN.md §7.10 I-1·M-D, §7.11, §4.2).

AST만 본다(실제 import 없음). 검사 항목
1. 자동발송 승인 값(`ApprovedBy.POLICY_AUTO_SEND`)을 참조하는 곳은 정의(core/models.py)·I8 초기화
   (storage/transitions.py)·조회/드레인(storage/repositories/autoreply.py)뿐이고, 그 모듈 안에서도
   G3·G4·G5·G7(잡 생성·실행·제출·결과 확정) 함수에는 등장하지 않는다 — Phase B에는 자동발송 행을
   만드는 코드가 없다(필수 테스트 ③, 리터럴 검사는 test_architecture_autoreply_keys.py).
2. `AutoSendVerifier` 구현(verify(conn, job, *, guard, preflight))은 rules/autosend_verify.py뿐.
3. `verifier=` 주입과 `AutoReplyRepository(` 생성은 app.py뿐.
4. `GuardResult(`·`PreflightResult(`·`AutoSendVerdict(` 생성 위치 제한.
5. `commit_autoreply_result`에 verify류 파라미터가 없고 모두 키워드 전용이다.
6. 의존 규칙: mcp_server는 autoreply를, autoreply는 rules를, rules·autoreply는 mail·ui·runtime·
   mcp_server를 import하지 않는다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src" / "emailtomcp"
REPO_AUTOREPLY = Path("storage/repositories/autoreply.py")


def _files() -> list[tuple[Path, ast.Module]]:
    result = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        result.append((path.relative_to(SRC), ast.parse(path.read_text(encoding="utf-8"))))
    return result


FILES = _files()


def _calls_named(tree: ast.AST, name: str) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else (func.attr if isinstance(func, ast.Attribute) else None)
            )
            if called == name:
                lines.append(node.lineno)
    return lines


def test_policy_autosend_enum_only_in_allowed_files() -> None:
    allowed = {Path("core/models.py"), Path("storage/transitions.py"), REPO_AUTOREPLY}
    offending = []
    for rel, tree in FILES:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "POLICY_AUTO_SEND"
                and rel not in allowed
            ):
                offending.append(f"{rel}:{node.lineno}")
    assert not offending, offending


def test_job_pipeline_functions_never_touch_autosend_approval() -> None:
    tree = dict(FILES)[REPO_AUTOREPLY]
    guarded = {
        "create_autoreply_job",
        "claim_next_job",
        "mark_submitted",
        "record_evaluation_outcome",
        "AutoReplyRepository",
        "_end_job_cancelled",
        "_end_job_failed",
    }
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.ClassDef) and node.name in guarded:
            for inner in ast.walk(node):
                if id(inner) in docstrings:
                    continue
                if isinstance(inner, ast.Name) and inner.id in ("_POLICY_AUTO_SEND", "ApprovedBy"):
                    found.append(f"{node.name}:{inner.lineno}")
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    text = inner.value.casefold()
                    if "approved_by" in text and "insert" not in text:
                        found.append(f"{node.name}:{inner.lineno} {inner.value[:40]!r}")
                    if "'outbox'" in text or text == "outbox":
                        found.append(f"{node.name}:{inner.lineno} outbox")
    assert not found, found


def test_g7_insert_writes_status_draft_without_approval_columns() -> None:
    tree = dict(FILES)[REPO_AUTOREPLY]
    inserts = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "INSERT INTO drafts" in n.value
    ]
    joined = " ".join(
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    assert inserts, "G7 초안 INSERT를 찾지 못함"
    assert "'autoreply', ?, 'draft'" in joined
    assert "approved_by" not in " ".join(inserts)


def test_autosend_verifier_implementations_only_in_autosend_verify() -> None:
    impls = []
    for rel, tree in FILES:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "verify":
                        kwonly = {a.arg for a in item.args.kwonlyargs}
                        if {"guard", "preflight"} <= kwonly:
                            impls.append((str(rel), node.name))
    # core/ports.py의 Protocol 정의는 구현이 아니다.
    impls = [i for i in impls if i[0] != str(Path("core/ports.py"))]
    assert impls == [(str(Path("rules/autosend_verify.py")), "AutoSendVerifierImpl")]


def test_verifier_injection_and_repository_construction_only_in_app() -> None:
    injected, constructed = [], []
    for rel, tree in FILES:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(k.arg == "verifier" for k in node.keywords):
                injected.append(str(rel))
        if _calls_named(tree, "AutoReplyRepository"):
            constructed.append(str(rel))
    assert set(injected) == {"app.py"}
    assert set(constructed) == {"app.py"}


@pytest.mark.parametrize(
    ("type_name", "owner"),
    [
        ("GuardResult", Path("rules/output_guard.py")),
        ("PreflightResult", Path("autoreply/preflight.py")),
        ("AutoSendVerdict", Path("rules/autosend_verify.py")),
    ],
)
def test_result_types_are_constructed_only_by_owner(type_name: str, owner: Path) -> None:
    places = {str(rel) for rel, tree in FILES if _calls_named(tree, type_name)}
    assert places == {str(owner)}


def test_commit_autoreply_result_has_no_verify_parameter() -> None:
    tree = dict(FILES)[REPO_AUTOREPLY]
    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "commit_autoreply_result"
    ]
    assert len(funcs) == 1
    args = funcs[0].args
    names = [a.arg for a in args.args] + [a.arg for a in args.kwonlyargs]
    assert not any("verif" in n for n in names)
    assert [a.arg for a in args.args] == ["self"]  # 나머지는 모두 키워드 전용
    assert {a.arg for a in args.kwonlyargs} == {"job_id", "proposed", "guard", "preflight", "now"}
    assert all(d is None for d in args.kw_defaults)  # 기본값 없음


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else (func.attr if isinstance(func, ast.Attribute) else None)
            )
            if called == name:
                found.append(node)
    return found


def test_production_never_overrides_claude_config_dir() -> None:
    """Spinoza 34번 Info-3: `JobRunner(claude_config_dir=...)`는 테스트용 덮어쓰기다. 운영 코드가
    넘기면 점검 경로와 자식 claude의 실제 경로가 다시 어긋난다(H-1 재발).

    - `JobRunner(`는 app.py에서만 만들고, 그 호출에 `claude_config_dir` 키워드나 `**` 펼침이 없다.
    - `run_pre_checks(config_dir=...)`는 러너(job_runner.py)만 넘긴다(러너 생성자 값을 그대로 전달).
    """
    constructed: list[str] = []
    offending: list[str] = []
    for rel, tree in FILES:
        for call in _calls(tree, "JobRunner"):
            constructed.append(str(rel))
            for kw in call.keywords:
                if kw.arg is None or kw.arg == "claude_config_dir":
                    offending.append(f"{rel}:{call.lineno} {kw.arg or '**'}")
        for call in _calls(tree, "run_pre_checks"):
            for kw in call.keywords:
                if (kw.arg is None or kw.arg == "config_dir") and rel != Path(
                    "autoreply/job_runner.py"
                ):
                    offending.append(f"{rel}:{call.lineno} run_pre_checks {kw.arg or '**'}")
    assert set(constructed) == {"app.py"}
    assert not offending, offending


def _imports(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


@pytest.mark.parametrize(
    ("package", "forbidden"),
    [
        ("mcp_server", ("emailtomcp.autoreply",)),
        (
            "autoreply",
            (
                "emailtomcp.rules",
                "emailtomcp.mail",
                "emailtomcp.ui",
                "emailtomcp.runtime",
                "emailtomcp.mcp_server",
            ),
        ),
        (
            "rules",
            (
                "emailtomcp.autoreply",
                "emailtomcp.mail",
                "emailtomcp.ui",
                "emailtomcp.runtime",
                "emailtomcp.mcp_server",
            ),
        ),
        ("storage", ("emailtomcp.rules", "emailtomcp.autoreply", "emailtomcp.mail")),
    ],
)
def test_dependency_rules(package: str, forbidden: tuple[str, ...]) -> None:
    offending = []
    for rel, tree in FILES:
        if rel.parts[0] != package:
            continue
        for mod in _imports(tree):
            if any(mod == f or mod.startswith(f + ".") for f in forbidden):
                offending.append(f"{rel}: {mod}")
    assert not offending, offending
