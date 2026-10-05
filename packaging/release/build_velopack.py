"""PyInstaller onedir 산출물을 Velopack으로 패키징한다(`vpk pack`, DESIGN.md §14.3).

전제
- `pyinstaller packaging/emailtomcp.spec`으로 `dist/emailtomcp/`(onedir)가 만들어져 있다.
- Velopack CLI(`vpk`, .NET 전역 도구)가 설치돼 있다:
      dotnet tool install -g vpk --version 1.2.161
  파이썬 훅 패키지(`velopack`, `__main__.run_velopack_hook`)와 **같은 버전**을 쓴다
  (pyproject.toml `[project.optional-dependencies] release`).

동작
- 버전은 `emailtomcp._version`(hatch-vcs)에서 읽고 PEP 440 → SemVer2로 바꿔 `--packVersion`에
  넣는다(`1.0.1` → `1.0.1`, `1.0.1rc1` → `1.0.1-rc.1`, §14.3/§15.3). 개발 빌드면 중단한다.
- 채널은 `stable`(§14.3, rc 버전만 `rc`).
  출력: Setup.exe, full(+delta) nupkg, releases.stable.json 등.
- Windows 전용이다. macOS는 notarization 확보 전 자동설치를 하지 않으므로(§14.1, V6)
  CI가 onedir를 zip(app_zip)으로만 배포한다.
- 서명(Authenticode)은 하지 않는다(K4, §14.8 — 인증서 확보 시 vpk 서명 옵션 추가).

사용 예(레포 루트에서)
    python packaging/release/build_velopack.py --pack-dir dist/emailtomcp --output-dir vpk-out
    python packaging/release/build_velopack.py --dry-run     # 명령만 출력
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from release_common import ReleaseError, read_app_version, require_release_version, semver_for

PACK_ID = "EmailToMCP"
PACK_TITLE = "EmailToMCP"
PACK_AUTHORS = "UTInfo"
CHANNEL = "stable"
MAIN_EXE = "emailtomcp.exe"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ICON = REPO_ROOT / "src" / "emailtomcp" / "ui" / "resources" / "icons" / "app_icon.ico"


def build_vpk_command(
    vpk: str,
    *,
    version: str,
    pack_dir: Path,
    output_dir: Path,
    icon: Path | None = DEFAULT_ICON,
    channel: str = CHANNEL,
) -> list[str]:
    cmd = [
        vpk,
        "pack",
        "--packId",
        PACK_ID,
        "--packVersion",
        semver_for(version),
        "--packDir",
        str(pack_dir),
        "--mainExe",
        MAIN_EXE,
        "--packTitle",
        PACK_TITLE,
        "--packAuthors",
        PACK_AUTHORS,
        "--channel",
        channel,
        "--outputDir",
        str(output_dir),
    ]
    if icon is not None:
        cmd += ["--icon", str(icon)]
    return cmd


def channel_for(version: str) -> str:
    """rc 버전은 stable 피드에 섞이지 않게 `rc` 채널로 만든다(rc 배포 자체는 U2/P5)."""
    return "rc" if "rc" in require_release_version(version) else CHANNEL


def resolve_vpk(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("vpk")
    if found is None:
        raise ReleaseError(
            "vpk(Velopack CLI)를 찾을 수 없습니다."
            " 설치: dotnet tool install -g vpk --version 1.2.161"
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PyInstaller onedir를 vpk pack으로 패키징한다.")
    parser.add_argument("--pack-dir", default="dist/emailtomcp", help="PyInstaller onedir 폴더")
    parser.add_argument("--output-dir", default="vpk-out", help="vpk 출력 폴더")
    parser.add_argument("--version", help="버전(기본: emailtomcp._version.__version__)")
    parser.add_argument("--vpk", help="vpk 실행 파일 경로(기본: PATH에서 찾기)")
    parser.add_argument("--no-icon", action="store_true", help="--icon을 넘기지 않는다")
    parser.add_argument("--dry-run", action="store_true", help="실행하지 않고 명령만 출력")
    args = parser.parse_args(argv)

    try:
        version = require_release_version(args.version or read_app_version())
        pack_dir = Path(args.pack_dir)
        if not args.dry_run:
            if not (pack_dir / MAIN_EXE).is_file():
                raise ReleaseError(
                    f"{pack_dir / MAIN_EXE}가 없습니다 — PyInstaller 빌드를 먼저 하세요"
                )
            vpk = resolve_vpk(args.vpk)
        else:
            vpk = args.vpk or "vpk"
        cmd = build_vpk_command(
            vpk,
            version=version,
            pack_dir=pack_dir,
            output_dir=Path(args.output_dir),
            icon=None if args.no_icon else DEFAULT_ICON,
            channel=channel_for(version),
        )
    except (ReleaseError, ValueError) as exc:
        print(f"[build_velopack] 오류: {exc}", file=sys.stderr)
        return 1

    print("[build_velopack] " + " ".join(cmd))
    if args.dry_run:
        return 0
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, check=False)  # noqa: S603 — shell 미사용
    if result.returncode != 0:
        print(f"[build_velopack] vpk pack 실패(종료 코드 {result.returncode})", file=sys.stderr)
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
