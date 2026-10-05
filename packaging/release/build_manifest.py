"""서명 전 후보 매니페스트(`manifest-candidate.json`) 생성 (DESIGN.md §14.4, §14.7-4, §15.4-5).

CI(release.yml)가 빌드 산출물을 모은 뒤 실행한다. **서명은 하지 않는다** — 서명은
메인테이너가 오프라인 PC에서 `sign_release.py`로 한다(D-8, 비밀키는 CI에 두지 않음).

동작
1. 버전을 `emailtomcp._version`(hatch-vcs, §15.2)에서 읽는다(`--version`으로 덮어쓰기 가능).
   정규형이 아니거나 `.dev`/`+local`이면 중단. `--expect-tag`를 주면 태그와도 대조한다.
2. 산출물마다 sha256과 크기를 계산해 §14.4 스키마의 artifacts 항목을 만든다.
   - `--artifact PLATFORM:ARCH:KIND:PATH` — 파일 1개를 명시적으로 지정
   - `--scan PLATFORM:ARCH:DIR` — Velopack 출력 폴더 등을 접미사 규칙으로 분류(release_common)
3. `issued_at`/`expires`/`released_at`은 빌드 시각으로 **임시값**을 채운다(스키마 필수 필드).
   서명 스크립트가 서명 시점 값으로 다시 채운다.
4. 저장 전에 앱과 같은 pydantic strict 스키마(`extra="forbid"`)와 릴리스 정책으로 자체검증한다.

사용 예(레포 루트에서)
    python packaging/release/build_manifest.py \
        --scan windows:x64:artifacts/windows-x64 \
        --artifact macos:arm64:app_zip:artifacts/macos-arm64/EmailToMCP-1.0.1-macos-arm64.zip \
        --expect-tag v1.0.1 --notes "버그 수정" --output manifest-candidate.json
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from release_common import (
    APP_ID,
    CANDIDATE_FILENAME,
    CHANNEL_STABLE,
    KINDS_BY_PLATFORM,
    PLATFORMS,
    SCHEMA_VERSION,
    ArtifactSpec,
    ReleaseError,
    artifact_url,
    default_valid_until,
    encode_manifest,
    format_ts,
    parse_ts,
    read_app_version,
    release_page_url,
    require_release_version,
    scan_directory,
    semver_for,
    sha256_file,
    utc_now,
    validate_manifest_bytes,
)


def parse_artifact_arg(text: str) -> ArtifactSpec:
    """`PLATFORM:ARCH:KIND:PATH` (PATH에 `D:\\...`처럼 콜론이 있어도 된다)."""
    parts = text.split(":", 3)
    if len(parts) != 4 or not all(parts):
        raise ReleaseError(f"--artifact 형식은 PLATFORM:ARCH:KIND:PATH 입니다: {text!r}")
    platform, arch, kind, path = parts
    _check_platform_kind(platform, kind)
    return ArtifactSpec(platform=platform, arch=arch, kind=kind, path=Path(path))


def parse_scan_arg(text: str) -> tuple[str, str, Path]:
    """`PLATFORM:ARCH:DIR`."""
    parts = text.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise ReleaseError(f"--scan 형식은 PLATFORM:ARCH:DIR 입니다: {text!r}")
    platform, arch, directory = parts
    if platform not in PLATFORMS:
        raise ReleaseError(f"지원하지 않는 platform: {platform!r} (허용: {', '.join(PLATFORMS)})")
    return platform, arch, Path(directory)


def _check_platform_kind(platform: str, kind: str) -> None:
    if platform not in PLATFORMS:
        raise ReleaseError(f"지원하지 않는 platform: {platform!r} (허용: {', '.join(PLATFORMS)})")
    allowed = KINDS_BY_PLATFORM[platform]
    if kind not in allowed:
        raise ReleaseError(
            f"{platform}에 허용되지 않는 kind: {kind!r} (허용: {', '.join(allowed)})"
        )


def artifact_entry(spec: ArtifactSpec, version: str) -> dict[str, Any]:
    """파일을 읽어 §14.4 artifacts 항목 1개를 만든다."""
    _check_platform_kind(spec.platform, spec.kind)
    if not spec.path.is_file():
        raise ReleaseError(f"산출물 파일이 없습니다: {spec.path}")
    digest, size = sha256_file(spec.path)
    name = spec.path.name
    return {
        "platform": spec.platform,
        "arch": spec.arch,
        "kind": spec.kind,
        "name": name,
        "url": artifact_url(version, name),
        "size": size,
        "sha256": digest,
    }


def build_candidate(
    specs: list[ArtifactSpec],
    *,
    version: str,
    min_floor: str,
    security: bool = False,
    notes_summary: str = "",
    now: datetime | None = None,
    released_at: datetime | None = None,
) -> tuple[dict[str, Any], bytes]:
    """후보 매니페스트(dict, 직렬화 바이트)를 만들고 스키마·정책으로 자체검증한다."""
    require_release_version(version)
    require_release_version(min_floor)
    if not specs:
        raise ReleaseError("매니페스트에 넣을 산출물이 하나도 없습니다")
    names = [s.path.name for s in specs]
    if len(names) != len(set(names)):
        raise ReleaseError("파일명이 같은 산출물이 여러 개입니다(릴리스 자산 이름은 유일해야 함)")

    issued = now or utc_now()
    artifacts = [artifact_entry(spec, version) for spec in specs]
    manifest: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "app_id": APP_ID,
        "channel": CHANNEL_STABLE,
        # 아래 두 값은 임시값이다 — sign_release.py가 서명 시점 값으로 다시 채운다.
        "issued_at": format_ts(issued),
        "expires": format_ts(default_valid_until(issued)),
        "min_version_floor": min_floor,
        "latest": {
            "version": version,
            "released_at": format_ts(released_at or issued),
            "security": security,
            "release_page": release_page_url(version),
            "notes_summary": notes_summary,
            "artifacts": artifacts,
        },
    }
    data = encode_manifest(manifest)
    validate_manifest_bytes(data)  # 저장 전 자체검증(스키마 extra=forbid + 정책)
    return manifest, data


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="서명 전 후보 매니페스트(manifest-candidate.json)를 만든다(서명 안 함)."
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="PLATFORM:ARCH:KIND:PATH",
        help="산출물 파일 1개(여러 번 지정 가능). kind: windows=setup|velopack_full|"
        "velopack_delta|velopack_feed, macos=app_zip",
    )
    parser.add_argument(
        "--scan",
        action="append",
        default=[],
        metavar="PLATFORM:ARCH:DIR",
        help="폴더 안 파일을 접미사 규칙으로 분류해 추가(Velopack 출력 폴더 등)",
    )
    parser.add_argument("--version", help="버전(기본: emailtomcp._version.__version__)")
    parser.add_argument("--expect-tag", help="이 태그(vX.Y.Z)와 버전이 같아야 한다")
    parser.add_argument("--min-floor", default="1.0.0", help="min_version_floor(기본 1.0.0)")
    parser.add_argument("--security", action="store_true", help="보안 릴리스로 표시")
    parser.add_argument("--notes", default="", help="notes_summary(최대 2000자)")
    parser.add_argument("--released-at", help="released_at(ISO 8601, 기본: 지금)")
    parser.add_argument(
        "--output", default=CANDIDATE_FILENAME, help=f"출력 경로(기본 {CANDIDATE_FILENAME})"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        version = require_release_version(args.version or read_app_version())
        if args.expect_tag is not None and args.expect_tag != f"v{version}":
            raise ReleaseError(f"버전({version})이 태그({args.expect_tag})와 다릅니다")
        semver = semver_for(version)

        specs = [parse_artifact_arg(a) for a in args.artifact]
        for scan in args.scan:
            platform, arch, directory = parse_scan_arg(scan)
            found = scan_directory(platform, arch, directory, semver)
            if not found:
                raise ReleaseError(f"{directory}에서 매니페스트 대상 산출물을 찾지 못했습니다")
            specs.extend(found)

        _, data = build_candidate(
            specs,
            version=version,
            min_floor=args.min_floor,
            security=args.security,
            notes_summary=args.notes,
            released_at=parse_ts(args.released_at) if args.released_at else None,
        )
    except (ReleaseError, ValueError) as exc:
        print(f"[build_manifest] 오류: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    print(f"[build_manifest] 후보 매니페스트 저장: {output} (버전 {version}, 서명 전)")
    for spec in specs:
        print(f"  - {spec.platform}/{spec.arch}/{spec.kind}: {spec.path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
