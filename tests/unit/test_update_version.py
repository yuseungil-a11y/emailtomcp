"""update.version — 버전 비교 규칙(DESIGN.md §14.5, §14.6 경계값·정규화, §15.3)."""

from __future__ import annotations

import pytest

from emailtomcp.update.version import (
    is_canonical_release,
    is_dev_build,
    normalize_version,
    parse_version,
    pep440_to_semver2,
)

pytestmark = pytest.mark.unit

BOUNDARY_ORDER = ["1.0.0", "1.0.1", "1.0.9", "1.0.10", "1.1.0", "2.0.0rc1", "2.0.0"]


def test_boundary_sort_order() -> None:
    """§14.6 경계값 정렬: 1.0.0 < 1.0.1 < 1.0.9 < 1.0.10 < 1.1.0 < 2.0.0rc1 < 2.0.0."""
    parsed = [parse_version(v) for v in BOUNDARY_ORDER]
    assert all(p is not None for p in parsed)
    for lower, higher in zip(parsed, parsed[1:], strict=False):
        assert lower < higher  # type: ignore[operator]
    # 역순으로 섞어도 Version 정렬은 원래 순서가 된다.
    shuffled = list(reversed(BOUNDARY_ORDER))
    assert sorted(shuffled, key=lambda v: parse_version(v)) == BOUNDARY_ORDER


def test_string_comparison_trap_is_avoided() -> None:
    """문자열 비교면 '1.0.10' < '1.0.9'가 참이 된다 — Version 비교는 그 반대다(§14.6 #4)."""
    assert "1.0.10" < "1.0.9"  # 함정 확인
    assert parse_version("1.0.10") > parse_version("1.0.9")  # type: ignore[operator]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("v1.0.0", "1.0.0"), ("V1.2.3", "1.2.3"), ("2.0.0-rc.1", "2.0.0rc1"), ("1.0.0", "1.0.0")],
)
def test_normalization(raw: str, expected: str) -> None:
    assert normalize_version(raw) == expected


@pytest.mark.parametrize("raw", ["", "v", "latest", "1.0.0-banana", "../1.0.0", None, 100])
def test_invalid_versions_return_none_without_crash(raw: object) -> None:
    assert parse_version(raw) is None


@pytest.mark.parametrize("raw", ["1.0.0", "1.0.10", "2.0.0rc1", "10.20.30"])
def test_canonical_release_accepted(raw: str) -> None:
    assert is_canonical_release(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "v1.0.0",
        "1.0",
        "1.0.0.0",
        "01.0.0",
        "1.0.0-rc.1",
        "1.0.0a1",
        "1.0.0b1",
        "1.0.0.dev3",
        "1.0.0+local",
        "1.0.0.post1",
        "1!1.0.0",
        " 1.0.0",
    ],
)
def test_non_canonical_rejected(raw: str) -> None:
    assert not is_canonical_release(raw)


@pytest.mark.parametrize(
    ("raw", "dev"),
    [
        ("1.0.0", False),
        ("2.0.0rc1", False),
        ("1.0.1.dev3+gabc", True),
        ("1.0.0.dev0", True),
        ("1.0.0+unknown", True),
        ("unknown", True),
    ],
)
def test_is_dev_build(raw: str, dev: bool) -> None:
    assert is_dev_build(raw) is dev


@pytest.mark.parametrize(
    ("pep440", "semver"),
    [("1.0.0", "1.0.0"), ("2.0.0rc1", "2.0.0-rc.1"), ("1.2.3rc10", "1.2.3-rc.10")],
)
def test_semver2_conversion_roundtrip(pep440: str, semver: str) -> None:
    assert pep440_to_semver2(pep440) == semver
    assert normalize_version(semver) == pep440  # 역변환


def test_semver2_rejects_non_canonical() -> None:
    with pytest.raises(ValueError):
        pep440_to_semver2("1.0.0.dev1")
