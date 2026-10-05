"""오프라인 서명 스크립트 — 메인테이너 PC에서만 실행 (DESIGN.md §14.7-4, §14.7-5).

**이 스크립트는 CI에서 실행하지 않는다.** 비밀키는 메인테이너의 오프라인 USB에만 있다(D-8).

두 가지 모드
1. 신규 릴리스 서명(§14.7-4)
       python packaging/release/sign_release.py \
           --candidate manifest-candidate.json --artifacts-dir <draft 자산 폴더> \
           --key <USB의 비밀키 파일> [--out-dir release-out]
   - 후보의 산출물마다 artifacts-dir의 실제 파일로 크기·sha256을 **다시 계산해 대조**한다
     (하나라도 다르거나 없으면 중단, 2단계).
   - issued_at=지금, expires=지금+30일로 채워 최종 `stable.json`을 만든다.
2. 재서명(§14.7-5, 만료 임박 시 30일마다)
       python packaging/release/sign_release.py --resign <기존 stable.json> --key <비밀키>
   - 기존 `stable.json.minisig`(기본: 같은 폴더)로 **기존 매니페스트가 내장 키로 서명된 것인지
     먼저 검증**한다. 검증 안 되는 파일은 재서명하지 않는다(Pages 저장소에 끼어든 변조본을
     메인테이너가 무심코 재서명하는 것을 막는다).
   - `issued_at`/`expires`만 갱신하고 나머지 내용은 그대로 둔다.

공통 동작
- 서명은 `minisign` CLI를 subprocess로 호출한다. 비밀키 암호는 **minisign이 직접 터미널에서
  입력받는다** — 이 스크립트는 암호를 받지도, 넘기지도, 저장하지도 않는다. 표준입출력을
  가로채지 않으므로 minisign 프롬프트가 그대로 보인다.
- 비밀키 경로·암호는 어디에도 출력/기록하지 않는다(명령줄도 출력하지 않는다). 비밀키가 이
  git 작업트리 안에 있으면 실수로 커밋될 수 있으므로 시작 전에 거부한다.
- trusted comment는 §14.4 형식 `app=EmailToMCP channel=stable version=<v> issued=<ts>`.
- 서명 직후 **앱에 내장된 공개키(K1)와 앱의 검증기(verify_manifest)로 결과를 다시 검증**한다.
  다른 키로 서명했거나 형식이 어긋나면 결과 파일을 지우고 실패한다.
- 결과(`stable.json`, `stable.json.minisig`)는 `--out-dir`에만 쓴다. git 커밋/push는 하지
  않는다 — Pages 반영은 Newton(vcs-manager)에게 맡긴다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from release_common import (
    DEFAULT_VALID_DAYS,
    MANIFEST_FILENAME,
    SIGNATURE_FILENAME,
    ReleaseError,
    default_valid_until,
    encode_manifest,
    format_ts,
    sha256_file,
    trusted_comment_for,
    utc_now,
    validate_manifest_bytes,
)

from emailtomcp.core.errors import PermanentError, SecurityError
from emailtomcp.update.keys import TrustedKey, embedded_trusted_keys, verify_minisign
from emailtomcp.update.manifest import Manifest
from emailtomcp.update.state import TrustState
from emailtomcp.update.verifier import VerifyContext, verify_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = "release-out"

# (메시지 파일, 서명 파일, trusted comment) → 서명 파일을 만든다. 운영은 minisign CLI.
Signer = Callable[[Path, Path, str], None]


@dataclass(frozen=True, slots=True)
class PreparedManifest:
    data: bytes
    manifest: Manifest
    trusted_comment: str
    # 재서명 시 기존 issued_at(롤백 방지 확인용). 신규 서명이면 None.
    previous_issued_at: datetime | None = None


# ---------------------------------------------------------------------------
# 1) 후보 → 최종 매니페스트
# ---------------------------------------------------------------------------


def verify_artifacts(manifest: Manifest, artifacts_dir: Path) -> None:
    """후보의 산출물마다 실제 파일의 크기·sha256을 다시 계산해 대조한다(§14.7-4 2단계).

    하나라도 없거나 다르면 모든 불일치를 모아 `ReleaseError`로 중단한다.
    """
    if not artifacts_dir.is_dir():
        raise ReleaseError(f"산출물 폴더가 없습니다: {artifacts_dir}")
    problems: list[str] = []
    for art in manifest.latest.artifacts:
        path = artifacts_dir / art.name  # name은 스키마가 경로구분자·'..'를 금지한다
        if not path.is_file():
            problems.append(f"{art.name}: 파일 없음")
            continue
        digest, size = sha256_file(path)
        if size != art.size:
            problems.append(f"{art.name}: 크기 불일치(후보 {art.size}, 실제 {size})")
        if digest != art.sha256:
            problems.append(f"{art.name}: sha256 불일치(후보 {art.sha256}, 실제 {digest})")
    if problems:
        raise ReleaseError("산출물 대조 실패 — 서명하지 않습니다:\n  " + "\n  ".join(problems))


def _with_validity(base: dict[str, Any], now: datetime, days: int) -> PreparedManifest:
    doc = json.loads(json.dumps(base))  # 깊은 복사
    doc["issued_at"] = format_ts(now)
    doc["expires"] = format_ts(default_valid_until(now, days))
    data = encode_manifest(doc)
    manifest = validate_manifest_bytes(data)
    return PreparedManifest(
        data=data, manifest=manifest, trusted_comment=trusted_comment_for(manifest)
    )


def prepare_from_candidate(
    candidate_bytes: bytes,
    artifacts_dir: Path,
    *,
    now: datetime,
    days: int = DEFAULT_VALID_DAYS,
) -> PreparedManifest:
    """후보 매니페스트를 검증·대조하고 issued_at/expires를 채운 최종본을 만든다."""
    candidate = validate_manifest_bytes(candidate_bytes)
    verify_artifacts(candidate, artifacts_dir)
    base = json.loads(candidate_bytes.decode("utf-8"))
    return _with_validity(base, now, days)


# ---------------------------------------------------------------------------
# 2) 재서명(§14.7-5)
# ---------------------------------------------------------------------------


def prepare_resign(
    old_bytes: bytes,
    old_signature: bytes,
    *,
    now: datetime,
    trusted_keys: Sequence[TrustedKey],
    days: int = DEFAULT_VALID_DAYS,
) -> PreparedManifest:
    """기존 서명 매니페스트를 검증한 뒤 issued_at/expires만 갱신한 최종본을 만든다."""
    try:
        sig, _key = verify_minisign(old_bytes, old_signature, tuple(trusted_keys))
    except (PermanentError, SecurityError) as exc:
        raise ReleaseError(
            f"기존 매니페스트의 서명을 검증할 수 없습니다 — 재서명하지 않습니다: {exc}"
        ) from exc
    old = validate_manifest_bytes(old_bytes)
    if sig.trusted_comment != trusted_comment_for(old):
        raise ReleaseError("기존 서명의 trusted comment가 매니페스트 내용과 다릅니다")
    if now <= old.issued_at:
        raise ReleaseError(
            f"현재 시각({format_ts(now)})이 기존 issued_at({format_ts(old.issued_at)})보다"
            " 늦지 않습니다 — PC 시계를 확인하세요(클라이언트가 롤백으로 거부함)"
        )
    base = json.loads(old_bytes.decode("utf-8"))
    prepared = _with_validity(base, now, days)
    # issued_at/expires 외에는 바뀌지 않았는지 확인(방어적).
    before = {k: v for k, v in base.items() if k not in ("issued_at", "expires")}
    after = {
        k: v for k, v in json.loads(prepared.data).items() if k not in ("issued_at", "expires")
    }
    if before != after:
        raise ReleaseError("재서명 중 issued_at/expires 외의 내용이 바뀌었습니다(내부 오류)")
    return PreparedManifest(
        data=prepared.data,
        manifest=prepared.manifest,
        trusted_comment=prepared.trusted_comment,
        previous_issued_at=old.issued_at,
    )


# ---------------------------------------------------------------------------
# 3) 서명 + 서명 후 자체검증
# ---------------------------------------------------------------------------


def verify_signed_output(
    prepared: PreparedManifest,
    signature: bytes,
    *,
    trusted_keys: Sequence[TrustedKey],
    now: datetime,
) -> str:
    """서명 결과를 앱과 같은 방식으로 검증한다. 성공하면 사용된 키 라벨을 돌려준다."""
    try:
        sig, key = verify_minisign(prepared.data, signature, tuple(trusted_keys))
    except (PermanentError, SecurityError) as exc:
        raise ReleaseError(
            f"서명 결과 검증 실패 — 앱 내장 공개키로 검증되지 않습니다(다른 키로 서명?): {exc}"
        ) from exc
    if sig.trusted_comment != prepared.trusted_comment:
        raise ReleaseError("서명의 trusted comment가 기대값과 다릅니다")
    ctx = VerifyContext(
        current_version=prepared.manifest.latest.version,
        now=now,
        trust=TrustState(last_issued_at=prepared.previous_issued_at),
        trusted_keys=tuple(trusted_keys),
        apply_floor=False,
    )
    try:
        verify_manifest(prepared.data, signature, ctx, save_trust=None)
    except (PermanentError, SecurityError) as exc:
        raise ReleaseError(f"앱 검증기(verify_manifest)가 결과를 거부했습니다: {exc}") from exc
    return key.label


def sign_and_write(
    prepared: PreparedManifest,
    out_dir: Path,
    signer: Signer,
    *,
    trusted_keys: Sequence[TrustedKey],
    now: datetime,
) -> tuple[Path, Path, str]:
    """임시 이름으로 쓰고 서명·검증한 뒤 최종 이름으로 바꾼다. 실패하면 임시 파일을 지운다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    final_manifest = out_dir / MANIFEST_FILENAME
    final_signature = out_dir / SIGNATURE_FILENAME
    tmp_manifest = out_dir / f"{MANIFEST_FILENAME}.new"
    tmp_signature = out_dir / f"{MANIFEST_FILENAME}.new.minisig"
    for stale in (tmp_manifest, tmp_signature):
        stale.unlink(missing_ok=True)
    try:
        tmp_manifest.write_bytes(prepared.data)
        signer(tmp_manifest, tmp_signature, prepared.trusted_comment)
        if not tmp_signature.is_file():
            raise ReleaseError("서명 파일이 만들어지지 않았습니다")
        if tmp_manifest.read_bytes() != prepared.data:
            raise ReleaseError("서명 중 매니페스트 파일이 바뀌었습니다")
        label = verify_signed_output(
            prepared, tmp_signature.read_bytes(), trusted_keys=trusted_keys, now=now
        )
        os.replace(tmp_manifest, final_manifest)
        os.replace(tmp_signature, final_signature)
    finally:
        tmp_manifest.unlink(missing_ok=True)
        tmp_signature.unlink(missing_ok=True)
    return final_manifest, final_signature, label


# ---------------------------------------------------------------------------
# minisign CLI
# ---------------------------------------------------------------------------


def resolve_minisign(explicit: str | None) -> str:
    """minisign 실행 파일 경로. 지정값 → PATH 순으로 찾고, 없으면 설치 안내와 함께 중단."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ReleaseError(f"--minisign으로 지정한 파일이 없습니다: {explicit}")
        return str(path)
    found = shutil.which("minisign")
    if found is None:
        raise ReleaseError(
            "minisign을 찾을 수 없습니다. 설치하거나 --minisign 으로 경로를 지정하세요"
            " (Windows: scoop install minisign 또는 https://github.com/jedisct1/minisign/releases,"
            " macOS: brew install minisign)"
        )
    return found


def build_minisign_command(
    minisign: str, key: Path, message: Path, signature: Path, trusted_comment: str
) -> list[str]:
    """`minisign -S`(서명) 명령. 암호 관련 인자는 넣지 않는다(minisign이 직접 묻는다)."""
    return [
        minisign,
        "-S",
        "-s",
        str(key),
        "-m",
        str(message),
        "-x",
        str(signature),
        "-t",
        trusted_comment,
    ]


def minisign_signer(minisign: str, key: Path) -> Signer:
    def _sign(message: Path, signature: Path, trusted_comment: str) -> None:
        cmd = build_minisign_command(minisign, key, message, signature, trusted_comment)
        print("[sign_release] minisign을 실행합니다. 비밀키 암호를 입력하세요.", flush=True)
        # stdin/stdout/stderr를 가로채지 않는다 — 암호 프롬프트는 minisign이 직접 처리한다.
        # 명령줄(비밀키 경로 포함)은 출력하지 않는다.
        result = subprocess.run(cmd, check=False)  # noqa: S603 — shell 미사용, 인자 목록
        if result.returncode != 0:
            raise ReleaseError(f"minisign 서명이 실패했습니다(종료 코드 {result.returncode})")

    return _sign


def check_key_location(key: Path) -> None:
    """비밀키 파일이 있는지, 이 git 작업트리 안에 있지 않은지 확인한다(경로는 출력하지 않음)."""
    if not key.is_file():
        raise ReleaseError("--key로 지정한 비밀키 파일이 없습니다")
    resolved = key.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        raise ReleaseError(
            "비밀키 파일이 저장소 작업트리 안에 있습니다 — 실수로 커밋될 수 있으니"
            " 오프라인 USB 등 저장소 밖에서 지정하세요"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _summary(prepared: PreparedManifest) -> str:
    m = prepared.manifest
    lines = [
        f"  version          : {m.latest.version}",
        f"  min_version_floor: {m.min_version_floor}",
        f"  security         : {m.latest.security}",
        f"  issued_at        : {format_ts(m.issued_at)}",
        f"  expires          : {format_ts(m.expires)}",
        f"  release_page     : {m.latest.release_page}",
        f"  trusted comment  : {prepared.trusted_comment}",
        "  artifacts:",
    ]
    for a in m.latest.artifacts:
        lines.append(f"    - {a.slot:<28} {a.name} ({a.size} bytes)")
        lines.append(f"      sha256 {a.sha256}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="오프라인 PC에서 stable.json에 minisign 서명한다(CI에서 실행 금지)."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--candidate", help="CI가 만든 manifest-candidate.json")
    mode.add_argument("--resign", help="재서명할 기존 stable.json(§14.7-5)")
    parser.add_argument(
        "--artifacts-dir", help="draft 릴리스에서 내려받은 산출물 폴더(--candidate)"
    )
    parser.add_argument("--resign-signature", help="기존 서명 파일(기본: <--resign 경로>.minisig)")
    parser.add_argument("--key", required=True, help="minisign 비밀키 파일(오프라인 USB)")
    parser.add_argument("--minisign", help="minisign 실행 파일 경로(기본: PATH에서 찾기)")
    parser.add_argument(
        "--out-dir", default=DEFAULT_OUT_DIR, help=f"결과 폴더(기본 {DEFAULT_OUT_DIR})"
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_VALID_DAYS, help="유효기간 일수(기본 30)"
    )
    parser.add_argument("--yes", action="store_true", help="서명 전 확인 질문을 건너뛴다")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    now = utc_now()
    trusted_keys = embedded_trusted_keys()
    try:
        if not trusted_keys:
            raise ReleaseError("앱에 내장된 공개키가 없습니다(update/keys.py)")
        key = Path(args.key)
        check_key_location(key)
        minisign = resolve_minisign(args.minisign)

        if args.candidate:
            if not args.artifacts_dir:
                raise ReleaseError("--candidate에는 --artifacts-dir가 필요합니다")
            prepared = prepare_from_candidate(
                Path(args.candidate).read_bytes(),
                Path(args.artifacts_dir),
                now=now,
                days=args.days,
            )
            print("[sign_release] 산출물 sha256/크기 대조 통과")
        else:
            old_path = Path(args.resign)
            sig_path = Path(args.resign_signature or f"{old_path}.minisig")
            if not sig_path.is_file():
                raise ReleaseError(f"기존 서명 파일이 없습니다: {sig_path}")
            prepared = prepare_resign(
                old_path.read_bytes(),
                sig_path.read_bytes(),
                now=now,
                trusted_keys=trusted_keys,
                days=args.days,
            )
            print("[sign_release] 기존 매니페스트 서명 검증 통과(재서명 모드)")

        print("[sign_release] 서명할 매니페스트:")
        print(_summary(prepared))
        if not args.yes:
            answer = input("위 내용으로 서명할까요? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("[sign_release] 취소했습니다.")
                return 1

        out_dir = Path(args.out_dir)
        manifest_path, signature_path, label = sign_and_write(
            prepared,
            out_dir,
            minisign_signer(minisign, key),
            trusted_keys=trusted_keys,
            now=now,
        )
    except (ReleaseError, OSError) as exc:
        print(f"[sign_release] 오류: {exc}", file=sys.stderr)
        return 1

    print(f"[sign_release] 서명 완료 — 내장 공개키({label})와 앱 검증기로 확인했습니다.")
    print(f"  {manifest_path}")
    print(f"  {signature_path}")
    print(
        "\n다음 단계(이 스크립트는 git 작업을 하지 않습니다):\n"
        "  1. 위 두 파일을 GitHub Pages 소스의 manifest/ 폴더에 복사하세요"
        " (게시 주소: https://yuseungil-a11y.github.io/emailtomcp/manifest/).\n"
        "  2. 커밋/push는 Newton(vcs-manager)에게 요청하세요.\n"
        "  3. 신규 릴리스라면 draft 릴리스를 publish하세요(packaging/release/README.md)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
