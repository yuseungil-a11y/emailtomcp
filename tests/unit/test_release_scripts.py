"""릴리스 자동화 스크립트(packaging/release/) — 후보 매니페스트 생성, sha256 대조, 서명 흐름.

실제 minisign·vpk·GitHub API는 호출하지 않는다. 서명은 테스트 전용 키(fake_github_releases의
TestSigningKey)로 minisign 형식을 흉내 내는 가짜 signer를 주입해 검증한다 — 운영 키와 무관하다.
"""

from __future__ import annotations

import importlib
import json
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


def test_sign_release_cli_end_to_end_without_leaking_key_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data, downloaded = _candidate_bytes(tmp_path)
    candidate = _write(tmp_path / "manifest-candidate.json", data)
    key = _write(tmp_path / "usb" / "zz-secret-key-marker.key", b"not-a-real-key")
    seen: dict[str, object] = {}

    def fake_minisign_signer(minisign: str, key_path: Path):
        seen["key"] = key_path
        return _fake_signer()

    monkeypatch.setattr(sign_release, "embedded_trusted_keys", lambda: TRUSTED)
    monkeypatch.setattr(sign_release, "resolve_minisign", lambda explicit: "minisign")
    monkeypatch.setattr(sign_release, "minisign_signer", fake_minisign_signer)
    out = tmp_path / "release-out"
    rc = sign_release.main(
        [
            "--candidate",
            str(candidate),
            "--artifacts-dir",
            str(downloaded),
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
    assert (out / "stable.json").is_file() and (out / "stable.json.minisig").is_file()
    assert "zz-secret-key-marker" not in captured.out + captured.err
    assert "Newton" in captured.out

    # 같은 결과물을 --resign으로 재서명(시각을 뒤로 돌려 롤백 검사가 통과하도록 now를 조정)
    monkeypatch.setattr(
        sign_release,
        "utc_now",
        lambda: datetime.now(UTC).replace(microsecond=0) + timedelta(days=1),
    )
    rc = sign_release.main(
        ["--resign", str(out / "stable.json"), "--key", str(key), "--out-dir", str(out), "--yes"]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "zz-secret-key-marker" not in captured.out + captured.err


def test_sign_release_cli_requires_artifacts_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    key = _write(tmp_path / "k.key", b"x")
    monkeypatch.setattr(sign_release, "resolve_minisign", lambda explicit: "minisign")
    rc = sign_release.main(["--candidate", str(tmp_path / "c.json"), "--key", str(key), "--yes"])
    assert rc == 1
    assert "--artifacts-dir" in capsys.readouterr().err


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
