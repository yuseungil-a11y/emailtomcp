"""릴리스 자동화 스크립트(packaging/release/) — 후보 매니페스트 생성, sha256 대조, 서명 흐름.

실제 minisign·vpk·GitHub API는 호출하지 않는다. 서명은 테스트 전용 키(fake_github_releases의
TestSigningKey)로 minisign 형식을 흉내 내는 가짜 signer를 주입해 검증한다 — 운영 키와 무관하다.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_DIR = REPO_ROOT / "packaging" / "release"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
# packaging/ 은 PyPI `packaging`과 이름이 겹치므로 패키지로 import하지 않고 폴더를 경로에 넣는다.
sys.path.insert(0, str(RELEASE_DIR))

from fake_github_releases import (  # noqa: E402
    TEST_KEY_ACTIVE,
    TEST_KEY_UNTRUSTED,
)

from emailtomcp.update.keys import verify_minisign  # noqa: E402
from emailtomcp.update.manifest import parse_manifest  # noqa: E402

common = importlib.import_module("release_common")
build_manifest = importlib.import_module("build_manifest")
sign_release = importlib.import_module("sign_release")
build_velopack = importlib.import_module("build_velopack")

ReleaseError = common.ReleaseError
ArtifactSpec = common.ArtifactSpec

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
TRUSTED = (TEST_KEY_ACTIVE.trusted_key("active"),)


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_artifacts(directory: Path, version: str = "1.0.1") -> list:
    win = directory / "win"
    mac = directory / "mac"
    return [
        ArtifactSpec(
            "windows", "x64", "setup", _write(win / "EmailToMCP-stable-Setup.exe", b"S" * 300)
        ),
        ArtifactSpec(
            "windows",
            "x64",
            "velopack_full",
            _write(win / f"EmailToMCP-{version}-stable-full.nupkg", b"F" * 500),
        ),
        ArtifactSpec(
            "windows", "x64", "velopack_feed", _write(win / "releases.stable.json", b"{}")
        ),
        ArtifactSpec(
            "macos",
            "arm64",
            "app_zip",
            _write(mac / f"EmailToMCP-{version}-macos-arm64.zip", b"Z" * 123),
        ),
    ]


def _candidate_bytes(tmp_path: Path, version: str = "1.0.1") -> tuple[bytes, Path]:
    specs = _make_artifacts(tmp_path / "build", version)
    _, data = build_manifest.build_candidate(
        specs, version=version, min_floor="1.0.0", notes_summary="버그 수정", now=NOW
    )
    # 메인테이너가 draft 릴리스에서 받은 것처럼 한 폴더에 평탄화한다.
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    for spec in specs:
        (downloaded / spec.path.name).write_bytes(spec.path.read_bytes())
    return data, downloaded


def _fake_signer(key=TEST_KEY_ACTIVE, comment_override: str | None = None):
    def _sign(message: Path, signature: Path, trusted_comment: str) -> None:
        signature.write_bytes(key.sign(message.read_bytes(), comment_override or trusted_comment))

    return _sign


# ---------------------------------------------------------------------------
# build_manifest.py — 후보 매니페스트
# ---------------------------------------------------------------------------


def test_candidate_matches_schema_and_hashes(tmp_path: Path) -> None:
    specs = _make_artifacts(tmp_path)
    doc, data = build_manifest.build_candidate(specs, version="1.0.1", min_floor="1.0.0", now=NOW)
    manifest = parse_manifest(data)  # 앱과 같은 strict 스키마(extra=forbid)
    assert manifest.latest.version == "1.0.1"
    assert manifest.latest.release_page.endswith("/releases/tag/v1.0.1")
    assert manifest.issued_at == NOW
    assert manifest.expires == NOW + timedelta(days=30)
    by_name = {a.name: a for a in manifest.latest.artifacts}
    for spec in specs:
        art = by_name[spec.path.name]
        digest, size = common.sha256_file(spec.path)
        assert (art.sha256, art.size) == (digest, size)
        assert art.url == (
            f"https://github.com/yuseungil-a11y/emailtomcp/releases/download/v1.0.1/{spec.path.name}"
        )
        assert (art.platform, art.arch, art.kind) == (spec.platform, spec.arch, spec.kind)
    # §14.4 형식: 키 정렬 + LF + UTF-8
    assert b"\r" not in data and data.endswith(b"\n")
    assert data == common.encode_manifest(json.loads(data))
    assert json.loads(data) == doc


@pytest.mark.parametrize("version", ["1.0.1.dev3+gabc", "1.0.1+local", "v1.0.1", "1.0", "01.0.0"])
def test_candidate_rejects_non_release_version(tmp_path: Path, version: str) -> None:
    with pytest.raises(ReleaseError):
        build_manifest.build_candidate(
            _make_artifacts(tmp_path), version=version, min_floor="1.0.0", now=NOW
        )


def test_candidate_rejects_rc_for_stable_channel(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="rc"):
        build_manifest.build_candidate(
            _make_artifacts(tmp_path, "1.0.1rc1"), version="1.0.1rc1", min_floor="1.0.0", now=NOW
        )


def test_candidate_rejects_floor_above_latest(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="min_version_floor"):
        build_manifest.build_candidate(
            _make_artifacts(tmp_path), version="1.0.1", min_floor="1.0.2", now=NOW
        )


def test_candidate_rejects_empty_and_duplicate_slots(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="하나도"):
        build_manifest.build_candidate([], version="1.0.1", min_floor="1.0.0", now=NOW)
    a = _write(tmp_path / "a" / "A-Setup.exe", b"a")
    b = _write(tmp_path / "b" / "B-Setup.exe", b"b")
    specs = [ArtifactSpec("windows", "x64", "setup", a), ArtifactSpec("windows", "x64", "setup", b)]
    with pytest.raises(ReleaseError, match="스키마"):
        build_manifest.build_candidate(specs, version="1.0.1", min_floor="1.0.0", now=NOW)


def test_candidate_rejects_bad_name_via_schema(tmp_path: Path) -> None:
    bad = _write(tmp_path / "with space-Setup.exe", b"x")
    with pytest.raises(ReleaseError, match="스키마"):
        build_manifest.build_candidate(
            [ArtifactSpec("windows", "x64", "setup", bad)],
            version="1.0.1",
            min_floor="1.0.0",
            now=NOW,
        )


def test_candidate_rejects_missing_file_and_bad_kind(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="없습니다"):
        build_manifest.build_candidate(
            [ArtifactSpec("windows", "x64", "setup", tmp_path / "nope.exe")],
            version="1.0.1",
            min_floor="1.0.0",
            now=NOW,
        )
    with pytest.raises(ReleaseError, match="허용되지 않는 kind"):
        build_manifest.parse_artifact_arg(f"macos:arm64:setup:{tmp_path / 'x.exe'}")
    with pytest.raises(ReleaseError, match="platform"):
        build_manifest.parse_artifact_arg("linux:x64:setup:x")


def test_parse_artifact_arg_keeps_windows_drive_colon() -> None:
    spec = build_manifest.parse_artifact_arg(r"windows:x64:setup:D:\out\EmailToMCP-Setup.exe")
    assert spec.path == Path(r"D:\out\EmailToMCP-Setup.exe")
    assert (spec.platform, spec.arch, spec.kind) == ("windows", "x64", "setup")


def test_classify_and_scan_velopack_output(tmp_path: Path) -> None:
    out = tmp_path / "vpk"
    for name in (
        "EmailToMCP-stable-Setup.exe",
        "EmailToMCP-1.0.1-stable-full.nupkg",
        "EmailToMCP-1.0.1-stable-delta.nupkg",
        "EmailToMCP-1.0.0-stable-full.nupkg",  # 이전 버전 잔재 → 제외
        "releases.stable.json",
        "assets.stable.json",
        "RELEASES-stable",
        "EmailToMCP-stable-Portable.zip",
        "sbom-pip-windows-x64.json",
    ):
        _write(out / name, b"x")
    specs = common.scan_directory("windows", "x64", out, "1.0.1")
    kinds = {s.path.name: s.kind for s in specs}
    assert kinds == {
        "EmailToMCP-stable-Setup.exe": "setup",
        "EmailToMCP-1.0.1-stable-full.nupkg": "velopack_full",
        "EmailToMCP-1.0.1-stable-delta.nupkg": "velopack_delta",
        "releases.stable.json": "velopack_feed",
    }
    assert common.classify_file("macos", "EmailToMCP-1.0.1-macos-arm64.zip", "1.0.1") == "app_zip"
    assert common.classify_file("macos", "sbom-pip-macos-arm64.json", "1.0.1") is None
    with pytest.raises(ReleaseError):
        common.scan_directory("windows", "x64", tmp_path / "missing", "1.0.1")


def test_build_manifest_cli_writes_candidate(tmp_path: Path) -> None:
    specs = _make_artifacts(tmp_path)
    output = tmp_path / "out" / "manifest-candidate.json"
    rc = build_manifest.main(
        [
            "--version",
            "1.0.1",
            "--expect-tag",
            "v1.0.1",
            "--scan",
            f"windows:x64:{tmp_path / 'win'}",
            "--artifact",
            f"macos:arm64:app_zip:{specs[3].path}",
            "--notes",
            "버그 수정",
            "--output",
            str(output),
        ]
    )
    assert rc == 0
    manifest = parse_manifest(output.read_bytes())
    assert {a.kind for a in manifest.latest.artifacts} == {
        "setup",
        "velopack_full",
        "velopack_feed",
        "app_zip",
    }
    assert manifest.latest.notes_summary == "버그 수정"


def test_build_manifest_cli_rejects_tag_mismatch_and_dev(tmp_path: Path, capsys) -> None:
    specs = _make_artifacts(tmp_path)
    out = tmp_path / "c.json"
    art = f"macos:arm64:app_zip:{specs[3].path}"
    assert (
        build_manifest.main(
            [
                "--version",
                "1.0.1",
                "--expect-tag",
                "v1.0.2",
                "--artifact",
                art,
                "--output",
                str(out),
            ]
        )
        == 1
    )
    assert (
        build_manifest.main(
            ["--version", "1.0.2.dev1+gabc", "--artifact", art, "--output", str(out)]
        )
        == 1
    )
    assert not out.exists()


# ---------------------------------------------------------------------------
# sign_release.py — sha256 대조, 최종본 생성, 서명 후 자체검증
# ---------------------------------------------------------------------------


def test_verify_artifacts_passes_on_exact_match(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    sign_release.verify_artifacts(parse_manifest(data), downloaded)


def test_verify_artifacts_reports_all_mismatches(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    # 같은 크기, 다른 해시
    (downloaded / "EmailToMCP-stable-Setup.exe").write_bytes(b"S" * 299 + b"X")
    (downloaded / "EmailToMCP-1.0.1-stable-full.nupkg").write_bytes(b"F" * 10)  # 크기 다름
    (downloaded / "releases.stable.json").unlink()  # 없음
    with pytest.raises(ReleaseError) as exc_info:
        sign_release.verify_artifacts(parse_manifest(data), downloaded)
    message = str(exc_info.value)
    assert "EmailToMCP-stable-Setup.exe: sha256 불일치" in message
    assert "EmailToMCP-1.0.1-stable-full.nupkg: 크기 불일치" in message
    assert "releases.stable.json: 파일 없음" in message


def test_prepare_from_candidate_sets_validity_and_comment(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    sign_time = NOW + timedelta(days=2, hours=3)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=sign_time)
    assert prepared.manifest.issued_at == sign_time
    assert prepared.manifest.expires == sign_time + timedelta(days=30)
    assert prepared.trusted_comment == (
        "app=EmailToMCP channel=stable version=1.0.1 issued=2026-11-03T03:00:00Z"
    )
    # issued_at/expires 외에는 후보와 같다.
    before, after = json.loads(data), json.loads(prepared.data)
    for key in ("issued_at", "expires"):
        before.pop(key), after.pop(key)
    assert before == after


def test_prepare_from_candidate_stops_on_mismatch(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    (downloaded / "releases.stable.json").write_bytes(b"[]")
    with pytest.raises(ReleaseError, match="대조 실패"):
        sign_release.prepare_from_candidate(data, downloaded, now=NOW)


def test_prepare_from_candidate_rejects_tampered_schema(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    doc = json.loads(data)
    doc["latest"]["unexpected"] = 1  # extra=forbid
    with pytest.raises(ReleaseError, match="스키마"):
        sign_release.prepare_from_candidate(common.encode_manifest(doc), downloaded, now=NOW)
    doc = json.loads(data)
    doc["latest"]["artifacts"][0]["url"] = "https://evil.example/x.exe"
    with pytest.raises(ReleaseError, match="스키마"):
        sign_release.prepare_from_candidate(common.encode_manifest(doc), downloaded, now=NOW)


def test_sign_and_write_produces_verifiable_pair(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)
    out = tmp_path / "release-out"
    manifest_path, sig_path, label = sign_release.sign_and_write(
        prepared, out, _fake_signer(), trusted_keys=TRUSTED, now=NOW
    )
    assert label == TEST_KEY_ACTIVE.label
    assert manifest_path.name == "stable.json" and sig_path.name == "stable.json.minisig"
    assert manifest_path.read_bytes() == prepared.data
    sig, _ = verify_minisign(manifest_path.read_bytes(), sig_path.read_bytes(), TRUSTED)
    assert sig.trusted_comment == prepared.trusted_comment
    assert sorted(p.name for p in out.iterdir()) == ["stable.json", "stable.json.minisig"]


def test_sign_and_write_rejects_wrong_key_and_leaves_nothing(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)
    out = tmp_path / "release-out"
    with pytest.raises(ReleaseError, match="내장 공개키"):
        sign_release.sign_and_write(
            prepared, out, _fake_signer(TEST_KEY_UNTRUSTED), trusted_keys=TRUSTED, now=NOW
        )
    assert list(out.iterdir()) == []


def test_sign_and_write_keeps_previous_files_on_failure(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)
    out = tmp_path / "release-out"
    _write(out / "stable.json", b"old")
    _write(out / "stable.json.minisig", b"old-sig")
    with pytest.raises(ReleaseError, match="trusted comment"):
        sign_release.sign_and_write(
            prepared,
            out,
            _fake_signer(comment_override="app=EmailToMCP channel=stable version=9.9.9 issued=x"),
            trusted_keys=TRUSTED,
            now=NOW,
        )
    assert (out / "stable.json").read_bytes() == b"old"
    assert (out / "stable.json.minisig").read_bytes() == b"old-sig"
    assert sorted(p.name for p in out.iterdir()) == ["stable.json", "stable.json.minisig"]


def test_sign_and_write_fails_when_signer_writes_nothing(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)
    with pytest.raises(ReleaseError, match="서명 파일"):
        sign_release.sign_and_write(
            prepared, tmp_path / "o", lambda m, s, c: None, trusted_keys=TRUSTED, now=NOW
        )


# ---------------------------------------------------------------------------
# 재서명(§14.7-5)
# ---------------------------------------------------------------------------


def _signed_pair(tmp_path: Path, when: datetime = NOW, key=TEST_KEY_ACTIVE) -> tuple[bytes, bytes]:
    data, downloaded = _candidate_bytes(tmp_path)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=when)
    return prepared.data, key.sign(prepared.data, prepared.trusted_comment)


def test_resign_updates_only_validity(tmp_path: Path) -> None:
    old_data, old_sig = _signed_pair(tmp_path)
    later = NOW + timedelta(days=23)
    prepared = sign_release.prepare_resign(old_data, old_sig, now=later, trusted_keys=TRUSTED)
    assert prepared.previous_issued_at == NOW
    assert prepared.manifest.issued_at == later
    assert prepared.manifest.expires == later + timedelta(days=30)
    old_doc, new_doc = json.loads(old_data), json.loads(prepared.data)
    for key in ("issued_at", "expires"):
        old_doc.pop(key), new_doc.pop(key)
    assert old_doc == new_doc
    # 서명까지 하면 롤백 검사(이전 issued_at 기준)를 포함한 앱 검증기를 통과한다.
    _, sig_path, _ = sign_release.sign_and_write(
        prepared, tmp_path / "out", _fake_signer(), trusted_keys=TRUSTED, now=later
    )
    assert sig_path.is_file()


def test_resign_refuses_tampered_or_foreign_manifest(tmp_path: Path) -> None:
    old_data, old_sig = _signed_pair(tmp_path)
    tampered = old_data.replace(b'"security": false', b'"security": true')
    assert tampered != old_data
    with pytest.raises(ReleaseError, match="재서명하지 않습니다"):
        sign_release.prepare_resign(
            tampered, old_sig, now=NOW + timedelta(days=1), trusted_keys=TRUSTED
        )
    foreign_data, foreign_sig = _signed_pair(tmp_path / "f", key=TEST_KEY_UNTRUSTED)
    with pytest.raises(ReleaseError, match="재서명하지 않습니다"):
        sign_release.prepare_resign(
            foreign_data, foreign_sig, now=NOW + timedelta(days=1), trusted_keys=TRUSTED
        )
    with pytest.raises(ReleaseError, match="재서명하지 않습니다"):
        sign_release.prepare_resign(old_data, b"garbage", now=NOW, trusted_keys=TRUSTED)


def test_resign_refuses_clock_behind_previous_issue(tmp_path: Path) -> None:
    old_data, old_sig = _signed_pair(tmp_path)
    with pytest.raises(ReleaseError, match="시계"):
        sign_release.prepare_resign(old_data, old_sig, now=NOW, trusted_keys=TRUSTED)


def test_resign_refuses_comment_mismatch(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)
    bad_sig = TEST_KEY_ACTIVE.sign(
        prepared.data, "app=EmailToMCP channel=stable version=1.0.0 issued=x"
    )
    with pytest.raises(ReleaseError, match="trusted comment"):
        sign_release.prepare_resign(
            prepared.data, bad_sig, now=NOW + timedelta(days=1), trusted_keys=TRUSTED
        )


# ---------------------------------------------------------------------------
# minisign 호출·비밀키 취급
# ---------------------------------------------------------------------------


def test_minisign_command_has_no_password_arguments(tmp_path: Path) -> None:
    cmd = sign_release.build_minisign_command(
        "minisign", tmp_path / "k.key", tmp_path / "m.json", tmp_path / "m.json.minisig", "tc"
    )
    assert cmd == [
        "minisign",
        "-S",
        "-s",
        str(tmp_path / "k.key"),
        "-m",
        str(tmp_path / "m.json"),
        "-x",
        str(tmp_path / "m.json.minisig"),
        "-t",
        "tc",
    ]
    assert "-W" not in cmd  # 암호 없는 키 생성/서명 옵션을 쓰지 않는다


def test_resolve_minisign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ReleaseError, match="없습니다"):
        sign_release.resolve_minisign(str(tmp_path / "missing.exe"))
    exe = _write(tmp_path / "minisign.exe", b"")
    assert sign_release.resolve_minisign(str(exe)) == str(exe)
    monkeypatch.setattr(sign_release.shutil, "which", lambda name: None)
    with pytest.raises(ReleaseError, match="찾을 수 없습니다"):
        sign_release.resolve_minisign(None)


def test_key_location_rules(tmp_path: Path) -> None:
    secret_name = "very-secret-key-name.key"
    with pytest.raises(ReleaseError) as missing:
        sign_release.check_key_location(tmp_path / secret_name)
    assert secret_name not in str(missing.value)  # 경로를 메시지에 남기지 않는다
    outside = _write(tmp_path / secret_name, b"x")
    sign_release.check_key_location(outside)
    inside = REPO_ROOT / "pyproject.toml"  # 저장소 안 파일(비밀키 대역)
    with pytest.raises(ReleaseError, match="작업트리"):
        sign_release.check_key_location(inside)


def test_key_location_rejects_any_parent_git_tree(tmp_path: Path) -> None:
    # L-2: 이 저장소가 아니어도 상위 폴더 어디에든 .git이 있으면(다른 clone, Pages 소스 등) 거부
    other_clone = tmp_path / "pages-src"
    (other_clone / ".git").mkdir(parents=True)
    key = _write(other_clone / "deep" / "dir" / "k1.key", b"x")
    with pytest.raises(ReleaseError, match="git 작업트리") as exc:
        sign_release.check_key_location(key)
    assert "k1.key" not in str(exc.value)
    # worktree/submodule은 .git이 파일이다 — 이것도 거부
    worktree = tmp_path / "wt"
    _write(worktree / ".git", b"gitdir: elsewhere\n")
    with pytest.raises(ReleaseError, match="git 작업트리"):
        sign_release.check_key_location(_write(worktree / "k.key", b"x"))


def test_gitignore_ignores_key_files() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.key" in lines


# ---------------------------------------------------------------------------
# 보안검토 44 — CLI 공통 가짜 환경
# ---------------------------------------------------------------------------


class _FakeGh:
    """`gh attestation verify` / `gh release list` 가짜. 실제 gh·네트워크는 쓰지 않는다."""

    def __init__(self, *, releases: list | None = None, fail_attest: tuple[str, ...] = ()) -> None:
        self.releases = releases if releases is not None else []
        self.fail_attest = fail_attest
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd[1:3] == ["attestation", "verify"]:
            failed = Path(cmd[3]).name in self.fail_attest
            return subprocess.CompletedProcess(
                cmd,
                1 if failed else 0,
                stdout="",
                stderr="no matching attestations" if failed else "",
            )
        if cmd[1:3] == ["release", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(self.releases), stderr="")
        raise AssertionError(f"예상하지 못한 gh 호출: {cmd}")

    def attested(self) -> list[str]:
        return [Path(c[3]).name for c in self.calls if c[1:3] == ["attestation", "verify"]]


def _pin_test_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """운영 K1 대신 테스트 키를 '고정 키'로 쓴다(운영 상수 자체는 별도 테스트로 검증)."""
    monkeypatch.setattr(sign_release, "PINNED_PUBLIC_KEY", TEST_KEY_ACTIVE.public_key_line())
    monkeypatch.setattr(sign_release, "PINNED_KEY_ID", TRUSTED[0].key_id_hex)
    monkeypatch.setattr(sign_release, "embedded_trusted_keys", lambda: TRUSTED)


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch,
    gh: _FakeGh,
    *,
    published: tuple[bytes | None, bytes | None] = (None, None),
    seen: dict | None = None,
) -> None:
    _pin_test_key(monkeypatch)

    def fake_minisign_signer(minisign: str, key_path: Path):
        if seen is not None:
            seen["key"] = key_path
        return _fake_signer()

    monkeypatch.setattr(sign_release, "resolve_minisign", lambda explicit: "minisign")
    monkeypatch.setattr(sign_release, "minisign_signer", fake_minisign_signer)
    monkeypatch.setattr(sign_release, "resolve_gh", lambda explicit: "gh")
    monkeypatch.setattr(sign_release, "run_command", gh)
    # 서버 시각 = 로컬 시각(시계 정상). utc_now를 바꾸는 테스트도 따라가도록 매번 호출한다.
    monkeypatch.setattr(sign_release, "fetch_server_date", lambda url: sign_release.utc_now())
    urls = {
        sign_release.PUBLISHED_MANIFEST_URL: published[0],
        sign_release.PUBLISHED_SIGNATURE_URL: published[1],
    }
    monkeypatch.setattr(sign_release, "fetch_url", lambda url: urls[url])


def _run_dirs(out: Path) -> list[Path]:
    return sorted(p for p in out.iterdir() if p.is_dir()) if out.is_dir() else []


def test_sign_release_cli_end_to_end_without_leaking_key_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    candidate = _write(tmp_path / "manifest-candidate.json", data)
    key = _write(tmp_path / "usb" / "zz-secret-key-marker.key", b"not-a-real-key")
    seen: dict[str, object] = {}
    gh = _FakeGh(
        releases=[
            {
                "tagName": "v1.0.1",
                "publishedAt": "2026-11-02T00:00:00Z",
                "isDraft": False,
                "isPrerelease": False,
            },
        ]
    )
    _patch_cli(monkeypatch, gh, seen=seen)
    out = tmp_path / "release-out"
    rc = sign_release.main(
        [
            "--candidate",
            str(candidate),
            "--artifacts-dir",
            str(downloaded),
            "--expect-version",
            "1.0.1",
            "--key",
            str(key),
            "--out-dir",
            str(out),
            "--yes",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert seen["key"] == key
    # H-2: 후보 + 산출물 전부 빌드 증명 확인(태그 ref 고정)
    names = {a.name for a in parse_manifest(data).latest.artifacts}
    assert sorted(gh.attested()) == sorted(names | {"manifest-candidate.json"})
    for call in gh.calls:
        assert call[call.index("--source-ref") + 1] == "refs/tags/v1.0.1"
    # L-1: 실행마다 새 하위 폴더
    (run1,) = _run_dirs(out)
    assert run1.name.startswith("1.0.1-")
    assert (run1 / "stable.json").is_file() and (run1 / "stable.json.minisig").is_file()
    assert "zz-secret-key-marker" not in captured.out + captured.err
    assert "Newton" in captured.out

    # 같은 결과물을 --resign으로 재서명(시각을 뒤로 돌려 롤백 검사가 통과하도록 now를 조정)
    monkeypatch.setattr(
        sign_release,
        "utc_now",
        lambda: datetime.now(UTC).replace(microsecond=0) + timedelta(days=1),
    )
    rc = sign_release.main(
        [
            "--resign",
            str(run1 / "stable.json"),
            "--expect-version",
            "1.0.1",
            "--key",
            str(key),
            "--out-dir",
            str(out),
            "--yes",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "zz-secret-key-marker" not in captured.out + captured.err
    assert len(_run_dirs(out)) == 2  # 이전 실행 결과는 그대로, 새 폴더에 따로


def test_sign_release_cli_requires_artifacts_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    key = _write(tmp_path / "k.key", b"x")
    monkeypatch.setattr(sign_release, "resolve_minisign", lambda explicit: "minisign")
    rc = sign_release.main(
        [
            "--candidate",
            str(tmp_path / "c.json"),
            "--expect-version",
            "1.0.1",
            "--key",
            str(key),
            "--yes",
        ]
    )
    assert rc == 1
    assert "--artifacts-dir" in capsys.readouterr().err


def test_sign_release_cli_requires_expect_version(tmp_path: Path, capsys) -> None:
    key = _write(tmp_path / "k.key", b"x")
    for mode in (["--candidate", "c.json", "--artifacts-dir", "d"], ["--resign", "stable.json"]):
        with pytest.raises(SystemExit) as exc:
            sign_release.main([*mode, "--key", str(key), "--yes"])
        assert exc.value.code == 2
        assert "--expect-version" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# H-2: 버전·태그·입력값 3중 일치, 빌드 증명 필수, 확인 화면
# ---------------------------------------------------------------------------


def test_check_expected_version_requires_all_three_to_match(tmp_path: Path) -> None:
    data, _ = _candidate_bytes(tmp_path)
    manifest = parse_manifest(data)
    assert sign_release.check_expected_version(manifest, "1.0.1") == "v1.0.1"
    with pytest.raises(ReleaseError, match="expect-version"):
        sign_release.check_expected_version(manifest, "1.0.2")
    with pytest.raises(ReleaseError):
        sign_release.check_expected_version(manifest, "v1.0.1")  # 정규형 아님

    # 버전 필드는 1.0.1인데 자산 URL·릴리스 페이지가 다른 태그(v1.0.0)를 가리키는 후보
    doc = json.loads(data)
    doc["latest"]["release_page"] = common.release_page_url("1.0.0")
    first = doc["latest"]["artifacts"][0]
    first["url"] = common.artifact_url("1.0.0", first["name"])
    forged = parse_manifest(common.encode_manifest(doc))  # 스키마(접두사 검사)는 통과한다
    with pytest.raises(ReleaseError) as exc:
        sign_release.check_expected_version(forged, "1.0.1")
    assert "release_page" in str(exc.value) and first["name"] in str(exc.value)


def test_attestation_command_pins_repo_workflow_and_tag(tmp_path: Path) -> None:
    cmd = sign_release.build_attestation_command("gh", tmp_path / "a.exe", "1.0.1")
    assert cmd[:4] == ["gh", "attestation", "verify", str(tmp_path / "a.exe")]
    assert cmd[cmd.index("--repo") + 1] == "yuseungil-a11y/emailtomcp"
    assert (
        cmd[cmd.index("--signer-workflow") + 1]
        == "yuseungil-a11y/emailtomcp/.github/workflows/release.yml"
    )
    assert cmd[cmd.index("--source-ref") + 1] == "refs/tags/v1.0.1"
    assert "--deny-self-hosted-runners" in cmd


def test_verify_attestations_fails_closed(tmp_path: Path) -> None:
    files = [tmp_path / "a.exe", tmp_path / "b.zip"]
    sign_release.verify_attestations(files, "1.0.1", gh="gh", runner=_FakeGh())
    with pytest.raises(ReleaseError, match="b.zip"):
        sign_release.verify_attestations(
            files, "1.0.1", gh="gh", runner=_FakeGh(fail_attest=("b.zip",))
        )

    def broken(cmd):
        raise FileNotFoundError("gh")

    with pytest.raises(ReleaseError, match="빌드 증명"):
        sign_release.verify_attestations(files, "1.0.1", gh="gh", runner=broken)
    with pytest.raises(ReleaseError):
        sign_release.verify_attestations([], "1.0.1", gh="gh", runner=_FakeGh())


def test_cli_stops_when_attestation_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    candidate = _write(tmp_path / "manifest-candidate.json", data)
    key = _write(tmp_path / "usb" / "k.key", b"x")
    _patch_cli(monkeypatch, _FakeGh(fail_attest=("manifest-candidate.json",)))
    out = tmp_path / "out"
    rc = sign_release.main(
        [
            "--candidate",
            str(candidate),
            "--artifacts-dir",
            str(downloaded),
            "--expect-version",
            "1.0.1",
            "--key",
            str(key),
            "--out-dir",
            str(out),
            "--yes",
        ]
    )
    assert rc == 1
    assert "빌드 증명" in capsys.readouterr().err
    assert _run_dirs(out) == []


def test_cli_stops_on_expect_version_mismatch_before_attestation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    candidate = _write(tmp_path / "manifest-candidate.json", data)
    key = _write(tmp_path / "usb" / "k.key", b"x")
    gh = _FakeGh()
    _patch_cli(monkeypatch, gh)
    rc = sign_release.main(
        [
            "--candidate",
            str(candidate),
            "--artifacts-dir",
            str(downloaded),
            "--expect-version",
            "1.0.2",
            "--key",
            str(key),
            "--out-dir",
            str(tmp_path / "o"),
            "--yes",
        ]
    )
    assert rc == 1
    assert "expect-version" in capsys.readouterr().err
    assert gh.calls == []


def test_summary_shows_notes_released_at_and_urls(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)
    text = sign_release._summary(prepared)
    assert "버그 수정" in text  # notes_summary
    assert "released_at" in text and common.format_ts(prepared.manifest.latest.released_at) in text
    for art in prepared.manifest.latest.artifacts:
        assert art.url in text


def test_prepare_from_candidate_reverifies_final_manifest(tmp_path: Path, monkeypatch) -> None:
    # L-3: 후보와 최종본 둘 다 산출물 대조
    data, downloaded = _candidate_bytes(tmp_path)
    calls: list[str] = []
    real = sign_release.verify_artifacts
    monkeypatch.setattr(
        sign_release,
        "verify_artifacts",
        lambda m, d: (calls.append(m.issued_at.isoformat()), real(m, d))[1],
    )
    sign_release.prepare_from_candidate(data, downloaded, now=NOW + timedelta(hours=1))
    assert len(calls) == 2 and calls[0] != calls[1]


# ---------------------------------------------------------------------------
# M-2: 재서명 기준점 = 최신 게시 릴리스, 만료본 재서명 거부
# ---------------------------------------------------------------------------


def _rel(tag: str, when: str, *, draft: bool = False, pre: bool = False) -> dict:
    return {"tagName": tag, "publishedAt": when, "isDraft": draft, "isPrerelease": pre}


def test_latest_published_stable_tag_picks_most_recent_non_rc() -> None:
    gh = _FakeGh(
        releases=[
            _rel("v1.0.0", "2026-10-01T00:00:00Z"),
            _rel("v1.0.2", "2026-10-20T00:00:00Z"),
            _rel("v1.0.1", "2026-10-10T00:00:00Z"),
            _rel("v1.1.0rc1", "2026-10-25T00:00:00Z"),  # rc 태그
            _rel("v1.1.0", "2026-10-26T00:00:00Z", pre=True),  # prerelease 표시
            _rel("v1.2.0", "", draft=True),  # draft
        ]
    )
    assert sign_release.latest_published_stable_tag(gh="gh", runner=gh) == "v1.0.2"
    cmd = gh.calls[0]
    assert cmd[cmd.index("--repo") + 1] == "yuseungil-a11y/emailtomcp"
    assert "--exclude-drafts" in cmd and "--exclude-pre-releases" in cmd


def test_latest_published_stable_tag_errors() -> None:
    with pytest.raises(ReleaseError, match="없습니다"):
        sign_release.latest_published_stable_tag(gh="gh", runner=_FakeGh(releases=[]))

    def failing(cmd):
        return subprocess.CompletedProcess(cmd, 4, stdout="", stderr="auth required")

    with pytest.raises(ReleaseError, match="gh release list"):
        sign_release.latest_published_stable_tag(gh="gh", runner=failing)

    def garbage(cmd):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    with pytest.raises(ReleaseError, match="JSON"):
        sign_release.latest_published_stable_tag(gh="gh", runner=garbage)


def test_check_resign_baseline(tmp_path: Path) -> None:
    data, _ = _candidate_bytes(tmp_path)
    manifest = parse_manifest(data)
    sign_release.check_resign_baseline(manifest, "1.0.1", "v1.0.1")
    with pytest.raises(ReleaseError, match="expect-version"):
        sign_release.check_resign_baseline(manifest, "1.0.2", "v1.0.2")
    with pytest.raises(ReleaseError, match="replay"):
        sign_release.check_resign_baseline(manifest, "1.0.1", "v1.0.2")


def test_resign_refuses_expired_manifest(tmp_path: Path, capsys) -> None:
    old_data, old_sig = _signed_pair(tmp_path)
    expired_at = NOW + timedelta(days=30)
    with pytest.raises(ReleaseError, match="만료"):
        sign_release.prepare_resign(old_data, old_sig, now=expired_at, trusted_keys=TRUSTED)
    assert "경고" in capsys.readouterr().err
    # 만료 직전은 허용
    sign_release.prepare_resign(
        old_data, old_sig, now=expired_at - timedelta(seconds=1), trusted_keys=TRUSTED
    )


def test_cli_resign_refuses_old_release_replay(tmp_path: Path, monkeypatch, capsys) -> None:
    # 예전(정상 서명된) 1.0.1 게시본을 되살렸지만 실제 최신 게시 릴리스는 v1.0.2인 상황
    old_data, old_sig = _signed_pair(
        tmp_path, when=datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)
    )
    old = _write(tmp_path / "pages" / "stable.json", old_data)
    _write(tmp_path / "pages" / "stable.json.minisig", old_sig)
    key = _write(tmp_path / "usb" / "k.key", b"x")
    gh = _FakeGh(
        releases=[_rel("v1.0.1", "2026-10-01T00:00:00Z"), _rel("v1.0.2", "2026-10-20T00:00:00Z")]
    )
    _patch_cli(monkeypatch, gh)
    out = tmp_path / "out"
    rc = sign_release.main(
        [
            "--resign",
            str(old),
            "--expect-version",
            "1.0.1",
            "--key",
            str(key),
            "--out-dir",
            str(out),
            "--yes",
        ]
    )
    assert rc == 1
    assert "v1.0.2" in capsys.readouterr().err
    assert _run_dirs(out) == []


# ---------------------------------------------------------------------------
# M-3: 서버 시각 대조, 현재 게시본 기준 시각·버전 역행 금지
# ---------------------------------------------------------------------------


def test_check_clock_accepts_small_skew_and_rejects_large() -> None:
    server = NOW
    assert (
        sign_release.check_clock(NOW + timedelta(minutes=4), fetch_date=lambda url: server)
        == server
    )
    with pytest.raises(ReleaseError, match="어긋납니다"):
        sign_release.check_clock(NOW + timedelta(minutes=6), fetch_date=lambda url: server)
    with pytest.raises(ReleaseError, match="어긋납니다"):
        sign_release.check_clock(NOW - timedelta(hours=3), fetch_date=lambda url: server)


def test_check_clock_falls_back_and_fails_closed() -> None:
    first, second = sign_release.CLOCK_SOURCES[:2]

    def flaky(url: str) -> datetime:
        if url == first:
            raise OSError("down")
        return NOW

    assert sign_release.check_clock(NOW, fetch_date=flaky) == NOW

    def down(url: str) -> datetime:
        raise OSError("offline")

    with pytest.raises(ReleaseError, match="확인할 수 없어"):
        sign_release.check_clock(NOW, fetch_date=down)

    def no_header(url: str) -> datetime:
        raise ValueError("Date 헤더 없음")

    with pytest.raises(ReleaseError, match="확인할 수 없어"):
        sign_release.check_clock(NOW, fetch_date=no_header)


def test_check_against_published_skips_when_nothing_published(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)
    same = sign_release.check_against_published(prepared, None, None, now=NOW, trusted_keys=TRUSTED)
    assert same is prepared


def _published_pair(tmp_path: Path, version: str, when: datetime) -> tuple[bytes, bytes]:
    data, downloaded = _candidate_bytes(tmp_path, version)
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=when)
    return prepared.data, TEST_KEY_ACTIVE.sign(prepared.data, prepared.trusted_comment)


def test_check_against_published_enforces_time_and_version(tmp_path: Path) -> None:
    data, downloaded = _candidate_bytes(tmp_path / "new", "1.0.1")
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)

    # 게시본 1.0.0(어제) → 통과, 롤백 기준이 게시본 issued_at으로 설정된다
    pub, sig = _published_pair(tmp_path / "p1", "1.0.0", NOW - timedelta(days=1))
    result = sign_release.check_against_published(prepared, pub, sig, now=NOW, trusted_keys=TRUSTED)
    assert result.previous_issued_at == NOW - timedelta(days=1)
    # 같은 버전 재발행도 허용
    pub, sig = _published_pair(tmp_path / "p2", "1.0.1", NOW - timedelta(days=1))
    sign_release.check_against_published(prepared, pub, sig, now=NOW, trusted_keys=TRUSTED)

    # 로컬 시각이 게시본 issued_at보다 늦지 않음(시계 느림)
    pub, sig = _published_pair(tmp_path / "p3", "1.0.0", NOW + timedelta(hours=1))
    with pytest.raises(ReleaseError, match="issued_at"):
        sign_release.check_against_published(prepared, pub, sig, now=NOW, trusted_keys=TRUSTED)

    # 새 버전이 게시본보다 낮음
    pub, sig = _published_pair(tmp_path / "p4", "1.0.2", NOW - timedelta(days=1))
    with pytest.raises(ReleaseError, match="낮습니다"):
        sign_release.check_against_published(prepared, pub, sig, now=NOW, trusted_keys=TRUSTED)


def test_check_against_published_stops_when_published_signature_invalid(tmp_path: Path) -> None:
    """게시본이 200으로 내려왔지만(존재함) 서명이 깨지면 경고만 하고 넘어가지 않고 중단한다.

    서명 없이도 Pages의 stable.json/.minisig만 손상시키면 M-3 전체(시계·다운그레이드 방지)를
    매번 우회할 수 있던 틈을 막는 회귀 테스트.
    """
    data, downloaded = _candidate_bytes(tmp_path / "new", "1.0.1")
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)
    pub, _ = _published_pair(tmp_path / "p", "1.0.2", NOW + timedelta(days=1))
    foreign = TEST_KEY_UNTRUSTED.sign(pub, "x")
    with pytest.raises(ReleaseError, match="서명을 확인할 수 없습니다"):
        sign_release.check_against_published(prepared, pub, foreign, now=NOW, trusted_keys=TRUSTED)
    # 404(파일이 원래 없음, 최초 릴리스)는 공격 시나리오가 아니므로 그대로 비교를 생략한다
    # (test_check_against_published_skips_when_nothing_published에서 이미 확인함).


def test_check_against_published_stops_when_pair_mismatched(tmp_path: Path) -> None:
    """stable.json과 .minisig 중 하나만 404면 중단한다(L-A, Spinoza 보안검토).

    둘 다 404여야 "최초 릴리스"로 비교를 건너뛴다 — 한쪽만 지워서(예: .minisig만 삭제)
    최초 릴리스로 가장해 다운그레이드/issued_at 비교를 우회하는 경로를 막는 회귀 테스트.
    """
    data, downloaded = _candidate_bytes(tmp_path / "new", "1.0.1")
    prepared = sign_release.prepare_from_candidate(data, downloaded, now=NOW)
    pub, sig = _published_pair(tmp_path / "p", "1.0.0", NOW - timedelta(days=1))

    # stable.json만 200, .minisig는 404
    with pytest.raises(ReleaseError, match="짝이 맞지 않습니다"):
        sign_release.check_against_published(prepared, pub, None, now=NOW, trusted_keys=TRUSTED)

    # stable.json은 404, .minisig만 200
    with pytest.raises(ReleaseError, match="짝이 맞지 않습니다"):
        sign_release.check_against_published(prepared, None, sig, now=NOW, trusted_keys=TRUSTED)

    # 둘 다 404면 기존 동작 그대로 유지(최초 릴리스로 비교 생략)
    same = sign_release.check_against_published(prepared, None, None, now=NOW, trusted_keys=TRUSTED)
    assert same is prepared


def test_cli_stops_when_local_clock_is_off(tmp_path: Path, monkeypatch, capsys) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    candidate = _write(tmp_path / "manifest-candidate.json", data)
    key = _write(tmp_path / "usb" / "k.key", b"x")
    gh = _FakeGh()
    _patch_cli(monkeypatch, gh)
    monkeypatch.setattr(
        sign_release, "fetch_server_date", lambda url: sign_release.utc_now() - timedelta(hours=1)
    )
    rc = sign_release.main(
        [
            "--candidate",
            str(candidate),
            "--artifacts-dir",
            str(downloaded),
            "--expect-version",
            "1.0.1",
            "--key",
            str(key),
            "--out-dir",
            str(tmp_path / "o"),
            "--yes",
        ]
    )
    assert rc == 1
    assert "시계" in capsys.readouterr().err
    assert gh.calls == []


def test_cli_new_signing_rejects_older_than_published(tmp_path: Path, monkeypatch, capsys) -> None:
    data, downloaded = _candidate_bytes(tmp_path / "c")
    candidate = _write(tmp_path / "manifest-candidate.json", data)
    key = _write(tmp_path / "usb" / "k.key", b"x")
    pub = _published_pair(
        tmp_path / "p", "1.0.2", datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    )
    _patch_cli(monkeypatch, _FakeGh(), published=pub)
    out = tmp_path / "o"
    rc = sign_release.main(
        [
            "--candidate",
            str(candidate),
            "--artifacts-dir",
            str(downloaded),
            "--expect-version",
            "1.0.1",
            "--key",
            str(key),
            "--out-dir",
            str(out),
            "--yes",
        ]
    )
    assert rc == 1
    assert "낮습니다" in capsys.readouterr().err
    assert _run_dirs(out) == []


# ---------------------------------------------------------------------------
# M-4: 신뢰 기준 공개키(K1) 스크립트 고정
# ---------------------------------------------------------------------------


def test_pinned_k1_matches_production_embedded_keys() -> None:
    from emailtomcp.update.keys import embedded_trusted_keys

    assert sign_release.PINNED_KEY_ID == "205BD649DF53346C"
    # 운영 keys.py의 내장 키가 고정값과 정확히 일치해야 실제 서명이 진행된다.
    sign_release.require_pinned_trusted_keys(embedded_trusted_keys())


def test_require_pinned_trusted_keys_rejects_any_deviation() -> None:
    from emailtomcp.update.keys import TrustedKey, embedded_trusted_keys

    (k1,) = embedded_trusted_keys()
    rogue = TEST_KEY_UNTRUSTED.trusted_key()
    with pytest.raises(ReleaseError, match="하나가 아닙니다"):
        sign_release.require_pinned_trusted_keys(())
    with pytest.raises(ReleaseError, match="하나가 아닙니다"):
        sign_release.require_pinned_trusted_keys((k1, rogue))  # 공격자 키 추가
    with pytest.raises(ReleaseError, match="key_id"):
        sign_release.require_pinned_trusted_keys((rogue,))  # 다른 키로 교체
    # key_id는 K1과 같게 위장했지만 공개키 바이트가 다른 키
    disguised = TrustedKey(label="K1", role="active", key_id=k1.key_id, public_key=rogue.public_key)
    assert disguised.key_id_hex == "205BD649DF53346C"
    with pytest.raises(ReleaseError, match="공개키 값"):
        sign_release.require_pinned_trusted_keys((disguised,))


def test_cli_stops_when_embedded_keys_differ_from_pin(tmp_path: Path, monkeypatch, capsys) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    candidate = _write(tmp_path / "manifest-candidate.json", data)
    key = _write(tmp_path / "usb" / "k.key", b"x")
    gh = _FakeGh()
    _patch_cli(monkeypatch, gh)
    # 작업트리 keys.py가 공격자 키를 추가로 내장한 상황(고정값은 테스트 키 그대로)
    monkeypatch.setattr(
        sign_release,
        "embedded_trusted_keys",
        lambda: (*TRUSTED, TEST_KEY_UNTRUSTED.trusted_key()),
    )
    rc = sign_release.main(
        [
            "--candidate",
            str(candidate),
            "--artifacts-dir",
            str(downloaded),
            "--expect-version",
            "1.0.1",
            "--key",
            str(key),
            "--out-dir",
            str(tmp_path / "o"),
            "--yes",
        ]
    )
    assert rc == 1
    assert "하나가 아닙니다" in capsys.readouterr().err
    assert gh.calls == []


# ---------------------------------------------------------------------------
# L-1: 실행마다 새 결과 폴더
# ---------------------------------------------------------------------------


def test_new_run_dir_is_unique(tmp_path: Path) -> None:
    run = sign_release.new_run_dir(tmp_path / "out", "1.0.1", NOW)
    assert run.name == "1.0.1-20261101T000000Z" and run.is_dir()
    with pytest.raises(ReleaseError, match="이미 있습니다"):
        sign_release.new_run_dir(tmp_path / "out", "1.0.1", NOW)


# ---------------------------------------------------------------------------
# H-1 / M-1 / L-4 / L-5: 워크플로 정적 검사(문법은 PyYAML로 파싱)
# ---------------------------------------------------------------------------


def _workflow(name: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _all_steps(wf: dict) -> list[tuple[str, dict]]:
    return [(job_id, step) for job_id, job in wf["jobs"].items() for step in job.get("steps", [])]


def test_release_workflow_installs_only_hash_pinned_dependencies() -> None:
    wf = _workflow("release.yml")
    for job_id, step in _all_steps(wf):
        run = step.get("run", "")
        for line in run.splitlines():
            line = line.strip()
            if "pip install" not in line:
                continue
            assert "--no-deps" in line, (job_id, line)
            assert "--require-hashes" in line or "--no-build-isolation -e ." in line, (job_id, line)
            assert "--upgrade" not in line, (job_id, line)
    locks = REPO_ROOT / "packaging" / "release" / "locks"
    for name in ("release", "build-backend", "manifest", "audit"):
        text = (locks / f"requirements-{name}.txt").read_text(encoding="utf-8")
        assert "--hash=sha256:" in text
    release_lock = (locks / "requirements-release.txt").read_text(encoding="utf-8")
    for pinned in (
        "pyinstaller==6.22.3",
        "velopack==1.2.161",
        "hatchling==",
        "hatch-vcs==",
        "editables==",
    ):
        assert pinned in release_lock
    runs = "\n".join(step.get("run", "") for _, step in _all_steps(wf))
    assert "pip_audit" in runs
    assert "sha256sum -c" in runs and "VPK_SHA256" in runs


def test_release_workflow_permission_split() -> None:
    wf = _workflow("release.yml")
    assert wf["permissions"] == {"contents": "read"}
    # 모든 checkout은 토큰을 남기지 않는다
    for job_id, step in _all_steps(wf):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False, job_id
    writers = [
        job_id
        for job_id, job in wf["jobs"].items()
        if job.get("permissions", {}).get("contents") == "write"
    ]
    assert writers == ["draft-release"]
    draft = wf["jobs"]["draft-release"]
    assert draft["environment"] == "release"
    assert draft["permissions"] == {"contents": "write"}
    uses = [str(s.get("uses", "")) for s in draft["steps"]]
    assert not any(u.startswith(("actions/checkout@", "actions/setup-python@")) for u in uses)
    import re

    # 쓰기 job에서는 파이썬/pip을 전혀 실행하지 않는다(pipefail 같은 단어는 제외하고 검사)
    assert not any(re.search(r"\bpip3?\b|\bpython3?\b", s.get("run", "")) for s in draft["steps"])
    assert not any("attest" in u for u in uses)
    # provenance는 build job(산출물)과 prepare-release(후보·SHA256SUMS)에서만
    attest_jobs = {
        job_id
        for job_id, step in _all_steps(wf)
        if "attest-build-provenance" in str(step.get("uses", ""))
    }
    assert attest_jobs == {"build", "prepare-release"}
    # L-4: 기존 릴리스가 있으면 --clobber로 덮지 않고 실패
    draft_run = "\n".join(s.get("run", "") for s in draft["steps"])
    assert "--clobber" not in draft_run and "exit 1" in draft_run


def test_workflows_pin_actions_by_sha() -> None:
    import re

    for name in ("release.yml", "ci.yml"):
        wf = _workflow(name)
        for job_id, step in _all_steps(wf):
            uses = step.get("uses")
            if uses:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), (name, job_id, uses)
    assert _workflow("ci.yml")["permissions"] == {"contents": "read"}


# ---------------------------------------------------------------------------
# build_velopack.py
# ---------------------------------------------------------------------------


def test_vpk_command_uses_semver2_and_channel(tmp_path: Path) -> None:
    cmd = build_velopack.build_vpk_command(
        "vpk",
        version="1.0.1rc2",
        pack_dir=tmp_path / "d",
        output_dir=tmp_path / "o",
        icon=None,
        channel=build_velopack.channel_for("1.0.1rc2"),
    )
    assert cmd[cmd.index("--packVersion") + 1] == "1.0.1-rc.2"
    assert cmd[cmd.index("--channel") + 1] == "rc"
    assert cmd[cmd.index("--mainExe") + 1] == "emailtomcp.exe"
    assert build_velopack.channel_for("1.0.1") == "stable"
    with pytest.raises(ReleaseError):
        build_velopack.channel_for("1.0.1.dev1")


def test_vpk_command_includes_explicit_runtime(tmp_path: Path) -> None:
    """`--runtime`이 없으면 vpk가 x86으로 기본설정해 UnauthorizedAccessException이 난다(로컬 재현,
    2026-10-05) — 환경에 의존하지 않도록 항상 win-x64를 명시한다."""
    cmd = build_velopack.build_vpk_command(
        "vpk",
        version="1.0.1",
        pack_dir=tmp_path / "d",
        output_dir=tmp_path / "o",
        icon=None,
        channel=build_velopack.CHANNEL,
    )
    assert cmd[cmd.index("--runtime") + 1] == "win-x64"


def test_build_velopack_dry_run(capsys) -> None:
    assert build_velopack.main(["--version", "1.0.1", "--dry-run", "--no-icon"]) == 0
    out = capsys.readouterr().out
    assert "--packVersion 1.0.1" in out and "--channel stable" in out
    assert build_velopack.main(["--version", "1.0.1.dev2+gabc", "--dry-run"]) == 1


# ---------------------------------------------------------------------------
# __main__ Velopack 훅(§14.3)
# ---------------------------------------------------------------------------


class _FakeVelopackApp:
    calls: list[tuple[str, object]] = []

    def set_auto_apply_on_startup(self, value: bool) -> _FakeVelopackApp:
        self.calls.append(("auto_apply", value))
        return self

    def run(self) -> None:
        self.calls.append(("run", None))


def test_velopack_hook_runs_only_in_frozen_build(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from emailtomcp import __main__ as entry

    fake = types.ModuleType("velopack")
    fake.App = _FakeVelopackApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "velopack", fake)
    _FakeVelopackApp.calls = []

    monkeypatch.delattr(sys, "frozen", raising=False)
    entry.run_velopack_hook()
    assert _FakeVelopackApp.calls == []  # 개발 환경에서는 아무 일도 하지 않는다

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    entry.run_velopack_hook()
    # 자동 적용은 끄고(신뢰 판단은 서명 매니페스트가 맡음) 훅만 실행한다.
    assert _FakeVelopackApp.calls == [("auto_apply", False), ("run", None)]


def test_velopack_hook_tolerates_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    from emailtomcp import __main__ as entry

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setitem(sys.modules, "velopack", None)  # import 시 ImportError
    entry.run_velopack_hook()  # 예외 없이 지나가야 한다
