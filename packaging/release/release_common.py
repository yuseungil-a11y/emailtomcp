"""릴리스 스크립트 공용 도우미 (DESIGN.md §14.4, §14.7, §15.4).

`build_manifest.py`(CI), `sign_release.py`(오프라인 PC), `build_velopack.py`(CI)가 같이 쓴다.
매니페스트 스키마·URL 규칙·버전 규칙은 **앱 코드(`emailtomcp.update.*`)를 그대로 재사용**한다 —
릴리스 도구와 클라이언트 검증기가 서로 다른 규칙을 갖게 되는 사고를 막기 위해서다.

주의: 이 폴더(`packaging/release/`)는 파이썬 패키지가 아니다. 레포 루트의 `packaging/`이 PyPI
`packaging` 라이브러리 이름과 겹치므로 `import packaging.release`로 쓰면 안 된다. 스크립트는
`python packaging/release/<스크립트>.py`로 실행하고(이때 sys.path[0]이 이 폴더라 형제 모듈을
바로 import할 수 있다), 테스트는 파일 경로로 로드한다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from emailtomcp.update.manifest import (
    APP_ID,
    CHANNEL_STABLE,
    RELEASES_URL_PREFIX,
    SCHEMA_VERSION,
    Manifest,
    parse_manifest,
)
from emailtomcp.update.version import is_canonical_release, parse_version, pep440_to_semver2

MAX_MANIFEST_BYTES = 64 * 1024  # §14.4 형식 규칙(verifier.MAX_MANIFEST_BYTES와 같은 값)
DEFAULT_VALID_DAYS = 30  # §14.7-5: expires 30일

CANDIDATE_FILENAME = "manifest-candidate.json"
MANIFEST_FILENAME = "stable.json"
SIGNATURE_FILENAME = "stable.json.minisig"

PLATFORMS = ("windows", "macos")
KINDS = ("setup", "velopack_full", "velopack_delta", "velopack_feed", "app_zip")
# 플랫폼별로 허용하는 kind(§14.4 예시: 윈도우 4종, macOS app_zip).
KINDS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "windows": ("setup", "velopack_full", "velopack_delta", "velopack_feed"),
    "macos": ("app_zip",),
}

_FEED_RE = re.compile(r"^releases\.[A-Za-z0-9_-]+\.json$")


class ReleaseError(Exception):
    """릴리스 도구가 작업을 중단해야 하는 오류(사용자에게 메시지를 그대로 보여준다)."""


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """매니페스트에 넣을 산출물 1개(로컬 파일 + platform/arch/kind)."""

    platform: str
    arch: str
    kind: str
    path: Path


# ---------------------------------------------------------------------------
# 해시/시간/직렬화
# ---------------------------------------------------------------------------


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """파일의 (sha256 소문자 hex, 바이트 크기)를 스트리밍으로 계산한다."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def utc_now() -> datetime:
    """초 단위로 자른 현재 UTC 시각."""
    return datetime.now(UTC).replace(microsecond=0)


def format_ts(value: datetime) -> str:
    """§14.4 예시 형식(`2026-11-01T00:00:00Z`). tz 없는 값은 거부한다."""
    if value.tzinfo is None:
        raise ReleaseError("시각에 시간대 정보가 없습니다")
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ReleaseError(f"시각에 시간대 정보가 없습니다: {text!r}")
    return parsed


def encode_manifest(manifest: dict[str, Any]) -> bytes:
    """§14.4 형식: UTF-8 JSON, 키 정렬, LF(테스트 fake의 encode_manifest와 같은 규칙)."""
    text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def validate_manifest_bytes(data: bytes) -> Manifest:
    """저장/서명 전 자체검증: 크기 상한 + 앱과 같은 pydantic strict 스키마(extra=forbid).

    스키마 위반은 `pydantic.ValidationError` 대신 `ReleaseError`로 바꿔 올린다.
    """
    from pydantic import ValidationError

    if len(data) > MAX_MANIFEST_BYTES:
        raise ReleaseError(f"매니페스트가 64KB 상한을 넘습니다({len(data)}바이트)")
    if b"\r" in data:
        raise ReleaseError("매니페스트에 CR 문자가 있습니다(LF만 허용)")
    try:
        manifest = parse_manifest(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ReleaseError(f"매니페스트 스키마 검증 실패: {details}") from exc
    check_release_policy(manifest)
    return manifest


def check_release_policy(manifest: Manifest) -> None:
    """스키마 밖의 릴리스 정책(클라이언트 verifier [4] 단계와 같은 조건 + floor 순서)."""
    if manifest.app_id != APP_ID:
        raise ReleaseError(f"app_id가 {APP_ID!r}가 아닙니다: {manifest.app_id!r}")
    if manifest.channel != CHANNEL_STABLE:
        raise ReleaseError(f"channel이 {CHANNEL_STABLE!r}가 아닙니다: {manifest.channel!r}")
    latest = parse_version(manifest.latest.version)
    floor = parse_version(manifest.min_version_floor)
    if latest is None or latest.is_prerelease:
        raise ReleaseError("stable 채널 매니페스트에는 rc 버전을 넣을 수 없습니다(rc 채널은 U2)")
    if floor is None or floor.is_prerelease:
        raise ReleaseError("min_version_floor는 rc 버전일 수 없습니다")
    if floor > latest:
        raise ReleaseError(
            f"min_version_floor({manifest.min_version_floor})가 latest({latest})보다 큽니다"
        )


def trusted_comment_for(manifest: Manifest) -> str:
    """§14.4 trusted comment: `app=EmailToMCP channel=stable version=<v> issued=<ts>`.

    `issued`는 JSON의 issued_at 문자열과 같은 값을 쓴다(클라이언트가 둘을 대조한다).
    """
    return (
        f"app={manifest.app_id} channel={manifest.channel} "
        f"version={manifest.latest.version} issued={format_ts(manifest.issued_at)}"
    )


# ---------------------------------------------------------------------------
# 버전
# ---------------------------------------------------------------------------


def read_app_version() -> str:
    """hatch-vcs가 만든 `emailtomcp._version`의 버전(§15.2 단일 버전 소스)."""
    from emailtomcp._version import __version__

    return str(__version__)


def require_release_version(version: str) -> str:
    """릴리스 버전 검증: PEP 440 정규형(`X.Y.Z`/`X.Y.ZrcN`)만, `.dev`/`+local` 금지."""
    if not is_canonical_release(version):
        raise ReleaseError(
            f"릴리스 버전이 아닙니다: {version!r} — 정규형(X.Y.Z 또는 X.Y.ZrcN)만 허용하며"
            " 개발 빌드(.dev, +local)는 릴리스할 수 없습니다"
        )
    return version


def semver_for(version: str) -> str:
    return pep440_to_semver2(require_release_version(version))


def release_page_url(version: str) -> str:
    return f"{RELEASES_URL_PREFIX}tag/v{version}"


def artifact_url(version: str, name: str) -> str:
    return f"{RELEASES_URL_PREFIX}download/v{version}/{name}"


# ---------------------------------------------------------------------------
# 산출물 분류(Velopack 출력 폴더 스캔)
# ---------------------------------------------------------------------------


def classify_file(platform: str, name: str, semver: str) -> str | None:
    """파일명으로 kind를 추정한다. 매니페스트 대상이 아니면 None.

    Velopack 출력 파일명은 채널/버전에 따라 조금씩 달라서(예: `EmailToMCP-win-Setup.exe`,
    `EmailToMCP-stable-Setup.exe`) 접미사 규칙으로만 판단한다. nupkg는 **이번 버전**
    (SemVer2 문자열)이 이름에 들어 있어야 한다 — 출력 폴더에 남은 이전 버전 패키지를
    잘못 싣는 것을 막는다.
    """
    lower = name.lower()
    if platform == "windows":
        if lower.endswith("-setup.exe"):
            return "setup"
        if lower.endswith("-full.nupkg"):
            return "velopack_full" if f"-{semver.lower()}-" in lower else None
        if lower.endswith("-delta.nupkg"):
            return "velopack_delta" if f"-{semver.lower()}-" in lower else None
        if _FEED_RE.fullmatch(name):
            return "velopack_feed"
        return None
    if platform == "macos":
        if lower.endswith(".zip") and "portable" not in lower:
            return "app_zip"
        return None
    return None


def scan_directory(platform: str, arch: str, directory: Path, semver: str) -> list[ArtifactSpec]:
    """폴더 안의 파일(하위 폴더 제외)을 분류해 ArtifactSpec 목록으로 만든다."""
    if not directory.is_dir():
        raise ReleaseError(f"산출물 폴더가 없습니다: {directory}")
    specs: list[ArtifactSpec] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        kind = classify_file(platform, path.name, semver)
        if kind is not None:
            specs.append(ArtifactSpec(platform=platform, arch=arch, kind=kind, path=path))
    return specs


def default_valid_until(issued_at: datetime, days: int = DEFAULT_VALID_DAYS) -> datetime:
    if days < 1 or days > 90:
        raise ReleaseError("유효기간(days)은 1~90일이어야 합니다")
    return issued_at + timedelta(days=days)


__all__ = [
    "APP_ID",
    "CANDIDATE_FILENAME",
    "CHANNEL_STABLE",
    "DEFAULT_VALID_DAYS",
    "KINDS",
    "KINDS_BY_PLATFORM",
    "MANIFEST_FILENAME",
    "MAX_MANIFEST_BYTES",
    "PLATFORMS",
    "SCHEMA_VERSION",
    "SIGNATURE_FILENAME",
    "ArtifactSpec",
    "ReleaseError",
    "artifact_url",
    "check_release_policy",
    "classify_file",
    "default_valid_until",
    "encode_manifest",
    "format_ts",
    "parse_ts",
    "read_app_version",
    "release_page_url",
    "require_release_version",
    "scan_directory",
    "semver_for",
    "sha256_file",
    "trusted_comment_for",
    "utc_now",
    "validate_manifest_bytes",
]
