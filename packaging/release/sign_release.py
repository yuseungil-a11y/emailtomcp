"""오프라인 서명 스크립트 — 메인테이너 PC에서만 실행 (DESIGN.md §14.7-4, §14.7-5).

**이 스크립트는 CI에서 실행하지 않는다.** 비밀키는 메인테이너의 오프라인 USB에만 있다(D-8).

두 가지 모드
1. 신규 릴리스 서명(§14.7-4)
       python packaging/release/sign_release.py \
           --candidate manifest-candidate.json --artifacts-dir <draft 자산 폴더> \
           --expect-version X.Y.Z --key <USB의 비밀키 파일> [--out-dir release-out]
   - 후보의 버전, 후보 URL 속 태그(`v<버전>`), 사람이 직접 입력한 `--expect-version` 세 값이
     모두 같아야 한다(보안검토 44 H-2).
   - 후보 매니페스트와 산출물마다 **`gh attestation verify`로 빌드 증명을 필수 확인**한다
     (서명 워크플로 = 이 저장소의 release.yml, source ref = `refs/tags/v<버전>`). 같은 draft에서
     받은 후보·산출물이 "서로만 맞는 악성 쌍"인 경우를 여기서 끊는다(H-2).
   - 후보의 산출물마다 artifacts-dir의 실제 파일로 크기·sha256을 **다시 계산해 대조**한다
     (하나라도 다르거나 없으면 중단, 2단계).
   - 현재 Pages에 게시된 stable.json이 있고 서명이 검증되면, 지금 시각이 그 issued_at보다 늦고
     새 버전이 그 버전 이상이어야 한다(M-3). 게시본이 없으면(404, 최초 릴리스) 이 검사는
     건너뛴다. 게시본이 있는데도 서명 검증이 안 되면(변조·손상 가능성) 경고만 남기지 않고
     **중단한다** — 비밀키 없이 Pages 파일만 손상시켜 M-3 전체를 우회하는 것을 막는다.
   - issued_at=지금, expires=지금+30일로 채워 최종 `stable.json`을 만든다.
2. 재서명(§14.7-5, 만료 임박 시 30일마다)
       python packaging/release/sign_release.py --resign <기존 stable.json> \
           --expect-version X.Y.Z --key <비밀키>
   - 기존 `stable.json.minisig`(기본: 같은 폴더)로 **기존 매니페스트가 내장 키로 서명된 것인지
     먼저 검증**한다. 검증 안 되는 파일은 재서명하지 않는다.
   - 기존 매니페스트 버전 == `--expect-version` == `gh release list`의 가장 최근 publish된
     비-rc 릴리스 태그여야 한다(M-2: 예전 정상 서명본을 되살려 재서명하는 replay 차단).
   - 기존 `expires`가 이미 지났으면 경고하고 중단한다(M-2).
   - `issued_at`/`expires`만 갱신하고 나머지 내용은 그대로 둔다.

공통 동작
- 시작 시 앱 내장 공개키 목록이 **이 스크립트에 고정한 K1(key_id 205BD649DF53346C) 하나와
  정확히 일치**하는지 확인한다(M-4). 작업트리의 keys.py가 바뀌었으면 중단한다.
- github.com 등의 HTTPS `Date` 헤더와 로컬 시계를 비교해 5분 넘게 어긋나면 중단한다(M-3).
- 서명은 `minisign` CLI를 subprocess로 호출한다. 비밀키 암호는 **minisign이 직접 터미널에서
  입력받는다** — 이 스크립트는 암호를 받지도, 넘기지도, 저장하지도 않는다. 표준입출력을
  가로채지 않으므로 minisign 프롬프트가 그대로 보인다.
- 비밀키 경로·암호는 어디에도 출력/기록하지 않는다(명령줄도 출력하지 않는다). 비밀키가
  git 작업트리 안(상위 폴더 어디든 `.git`이 있는 곳 포함)에 있으면 시작 전에 거부한다(L-2).
- trusted comment는 §14.4 형식 `app=EmailToMCP channel=stable version=<v> issued=<ts>`.
- 서명 직후 **앱에 내장된 공개키(K1)와 앱의 검증기(verify_manifest)로 결과를 다시 검증**한다.
  다른 키로 서명했거나 형식이 어긋나면 결과 파일을 지우고 실패한다.
- 결과(`stable.json`, `stable.json.minisig`)는 실행마다 새로 만드는 하위 폴더
  `--out-dir/<버전>-<UTC시각>/`에만 쓴다(L-1: 예전 실행 결과와 섞인 쌍이 게시되는 것 방지).
  git 커밋/push는 하지 않는다 — Pages 반영은 Newton(vcs-manager)에게 맡긴다.
- 네트워크(GitHub API, Pages)와 로그인된 `gh` CLI가 필요하다. 비밀키만 오프라인이다.
"""

from __future__ import annotations

import argparse
import dataclasses
import email.utils
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from release_common import (
    DEFAULT_VALID_DAYS,
    MANIFEST_FILENAME,
    MAX_MANIFEST_BYTES,
    SIGNATURE_FILENAME,
    ReleaseError,
    artifact_url,
    default_valid_until,
    encode_manifest,
    format_ts,
    release_page_url,
    require_release_version,
    sha256_file,
    trusted_comment_for,
    utc_now,
    validate_manifest_bytes,
)

from emailtomcp.core.errors import PermanentError, SecurityError
from emailtomcp.update.keys import (
    TrustedKey,
    embedded_trusted_keys,
    parse_public_key,
    verify_minisign,
)
from emailtomcp.update.manifest import Manifest
from emailtomcp.update.state import TrustState
from emailtomcp.update.verifier import VerifyContext, verify_manifest
from emailtomcp.update.version import parse_version

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = "release-out"

# ---------------------------------------------------------------------------
# 이 스크립트에 고정하는 신뢰 기준값 — 작업트리의 다른 파일에서 읽지 않는다(보안검토 44 M-4, H-2).
# 운영 키를 바꾸는 경우(§14.7-6)에는 keys.py와 함께 **이 값도 리뷰를 거쳐** 바꾼다.
# ---------------------------------------------------------------------------
PINNED_KEY_ID = "205BD649DF53346C"  # K1(active), minisign 표시 형식
PINNED_PUBLIC_KEY = "RWRsNFPfSdZbIOJCGCtFny5oT+uyBqtw+zTK/B8Ufs/GFX/Fi2M2K5nC"

GITHUB_REPO = "yuseungil-a11y/emailtomcp"
SIGNER_WORKFLOW = f"{GITHUB_REPO}/.github/workflows/release.yml"
PUBLISHED_MANIFEST_URL = "https://yuseungil-a11y.github.io/emailtomcp/manifest/stable.json"
PUBLISHED_SIGNATURE_URL = PUBLISHED_MANIFEST_URL + ".minisig"
# HTTPS 응답 Date 헤더로 로컬 시계를 확인할 주소(앞에서부터 시도, 하나만 성공하면 된다).
CLOCK_SOURCES = ("https://api.github.com/", "https://yuseungil-a11y.github.io/emailtomcp/")
MAX_CLOCK_SKEW = timedelta(minutes=5)
_STABLE_TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_HTTP_TIMEOUT_SEC = 15

# (메시지 파일, 서명 파일, trusted comment) → 서명 파일을 만든다. 운영은 minisign CLI.
Signer = Callable[[Path, Path, str], None]
# 명령 실행(출력 캡처). 운영은 subprocess, 테스트는 가짜를 주입한다.
CommandRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True, slots=True)
class PreparedManifest:
    data: bytes
    manifest: Manifest
    trusted_comment: str
    # 롤백 방지 기준 issued_at(재서명: 기존 파일, 신규: 현재 게시본). 없으면 None.
    previous_issued_at: datetime | None = None


# ---------------------------------------------------------------------------
# 0) 신뢰 기준 공개키 고정(M-4)
# ---------------------------------------------------------------------------


def require_pinned_trusted_keys(trusted_keys: Sequence[TrustedKey]) -> None:
    """앱 내장 공개키 목록이 고정한 K1 **하나와 정확히** 일치하는지 확인한다.

    비었거나, K1이 없거나, 다른 키가 섞였거나, key_id는 같은데 공개키 바이트가 다르면 중단한다
    (minisign key_id는 임의 8바이트라 key_id만으로는 위조를 막지 못하므로 공개키도 비교한다).
    """
    pinned = parse_public_key(PINNED_PUBLIC_KEY, label="pinned")
    if pinned.key_id_hex != PINNED_KEY_ID:
        raise ReleaseError("스크립트에 고정한 K1 key_id와 공개키가 서로 맞지 않습니다(내부 오류)")
    ids = [k.key_id_hex for k in trusted_keys]
    if len(trusted_keys) != 1:
        raise ReleaseError(
            f"앱 내장 공개키가 K1({PINNED_KEY_ID}) 하나가 아닙니다: {ids or '없음'}"
            " — update/keys.py가 바뀌었는지 확인하세요(서명하지 않습니다)"
        )
    key = trusted_keys[0]
    if key.key_id_hex != PINNED_KEY_ID:
        raise ReleaseError(
            f"앱 내장 공개키의 key_id({key.key_id_hex})가 고정값 {PINNED_KEY_ID}와 다릅니다"
        )
    if key.public_key.public_bytes_raw() != pinned.public_key.public_bytes_raw():
        raise ReleaseError(
            f"앱 내장 공개키(key_id {PINNED_KEY_ID})의 공개키 값이 고정값과 다릅니다"
            " — update/keys.py 변조 여부를 확인하세요"
        )


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
    prepared = _with_validity(base, now, days)
    # 실제로 서명할 최종본으로도 다시 대조한다(L-3, 방어적 — 후보 파싱과 최종본 파싱의
    # 차이로 산출물 목록이 달라지는 경우를 막는다).
    verify_artifacts(prepared.manifest, artifacts_dir)
    return prepared


def check_expected_version(manifest: Manifest, expect_version: str) -> str:
    """매니페스트 버전 · URL 속 태그 · 사람이 입력한 버전 세 값이 모두 같은지 확인한다(H-2).

    성공하면 태그 이름(`v<버전>`)을 돌려준다.
    """
    expected = require_release_version(expect_version)
    tag = f"v{expected}"
    problems: list[str] = []
    if manifest.latest.version != expected:
        problems.append(
            f"매니페스트 버전({manifest.latest.version})이 --expect-version({expected})과 다릅니다"
        )
    if manifest.latest.release_page != release_page_url(expected):
        problems.append(
            f"release_page가 태그 {tag}를 가리키지 않습니다: {manifest.latest.release_page}"
        )
    for art in manifest.latest.artifacts:
        if art.url != artifact_url(expected, art.name):
            problems.append(f"{art.name}: URL이 태그 {tag}의 자산이 아닙니다: {art.url}")
    if problems:
        raise ReleaseError(
            "버전/태그 일치 확인 실패 — 서명하지 않습니다:\n  " + "\n  ".join(problems)
        )
    return tag


# ---------------------------------------------------------------------------
# 1-1) 빌드 증명(provenance) 필수 확인(H-2)
# ---------------------------------------------------------------------------


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """명령을 실행하고 출력을 캡처한다(gh 전용 — 비밀값을 다루지 않는 명령에만 쓴다)."""
    return subprocess.run(  # noqa: S603 — shell 미사용, 인자 목록
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
    )


def resolve_gh(explicit: str | None) -> str:
    """GitHub CLI 경로. 지정값 → PATH 순으로 찾고, 없으면 설치 안내와 함께 중단."""
    if explicit:
        if not Path(explicit).is_file():
            raise ReleaseError(f"--gh로 지정한 파일이 없습니다: {explicit}")
        return explicit
    found = shutil.which("gh")
    if found is None:
        raise ReleaseError(
            "GitHub CLI(gh)를 찾을 수 없습니다 — 빌드 증명 확인에 필수입니다."
            " 설치 후 `gh auth login` 하거나 --gh 로 경로를 지정하세요"
        )
    return found


def build_attestation_command(gh: str, path: Path, version: str) -> list[str]:
    return [
        gh,
        "attestation",
        "verify",
        str(path),
        "--repo",
        GITHUB_REPO,
        "--signer-workflow",
        SIGNER_WORKFLOW,
        "--source-ref",
        f"refs/tags/v{version}",
        "--deny-self-hosted-runners",
    ]


def verify_attestations(
    paths: Sequence[Path], version: str, *, gh: str, runner: CommandRunner
) -> None:
    """파일마다 `gh attestation verify`를 실행한다. 하나라도 실패하면 모아서 중단한다."""
    if not paths:
        raise ReleaseError("빌드 증명을 확인할 파일이 없습니다")
    failures: list[str] = []
    for path in paths:
        try:
            result = runner(build_attestation_command(gh, path, version))
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{path.name}: gh 실행 실패({exc.__class__.__name__})")
            continue
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else f"종료 코드 {result.returncode}"
            failures.append(f"{path.name}: {tail}")
        else:
            print(f"[sign_release] 빌드 증명 확인: {path.name}", flush=True)
    if failures:
        raise ReleaseError(
            "빌드 증명(gh attestation verify) 확인 실패 — 서명하지 않습니다"
            f" (repo {GITHUB_REPO}, workflow release.yml, ref refs/tags/v{version}):\n  "
            + "\n  ".join(failures)
        )


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
    if old.expires <= now:
        # M-2: 만료된 파일은 Pages에서 내려간 예전 서명본일 수 있다(replay). 재서명으로
        # 되살리지 않는다 — 최신 릴리스의 후보 매니페스트로 신규 서명 절차를 다시 밟는다.
        print(
            "[sign_release] 경고: 기존 매니페스트가 이미 만료됐습니다"
            f"(expires {format_ts(old.expires)}).",
            file=sys.stderr,
        )
        raise ReleaseError(
            "만료된 매니페스트는 재서명하지 않습니다 — 최신 릴리스의 manifest-candidate.json으로"
            " 신규 서명(--candidate) 절차를 진행하세요"
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


def latest_published_stable_tag(*, gh: str, runner: CommandRunner) -> str:
    """`gh release list`에서 가장 최근에 publish된 비-rc(정식) 릴리스 태그를 찾는다(M-2)."""
    cmd = [
        gh,
        "release",
        "list",
        "--repo",
        GITHUB_REPO,
        "--exclude-drafts",
        "--exclude-pre-releases",
        "--limit",
        "100",
        "--json",
        "tagName,publishedAt,isDraft,isPrerelease",
    ]
    try:
        result = runner(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(f"gh release list 실행 실패: {exc.__class__.__name__}") from exc
    if result.returncode != 0:
        raise ReleaseError(
            f"gh release list 실패(종료 코드 {result.returncode}): {(result.stderr or '').strip()}"
        )
    try:
        releases = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ReleaseError("gh release list 출력이 JSON이 아닙니다") from exc
    best: tuple[datetime, str] | None = None
    for rel in releases if isinstance(releases, list) else []:
        if not isinstance(rel, dict) or rel.get("isDraft") or rel.get("isPrerelease"):
            continue
        tag, published = rel.get("tagName"), rel.get("publishedAt")
        if not isinstance(tag, str) or not _STABLE_TAG_RE.fullmatch(tag):
            continue
        if not isinstance(published, str) or not published:
            continue
        try:
            when = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            continue
        if best is None or when > best[0]:
            best = (when, tag)
    if best is None:
        raise ReleaseError("publish된 정식(비-rc) 릴리스가 없습니다 — 재서명할 기준이 없습니다")
    return best[1]


def check_resign_baseline(manifest: Manifest, expect_version: str, latest_tag: str) -> None:
    """재서명 대상 = --expect-version = 최신 게시 릴리스 태그 인지 확인한다(M-2)."""
    expected = require_release_version(expect_version)
    if manifest.latest.version != expected:
        raise ReleaseError(
            f"기존 매니페스트 버전({manifest.latest.version})이 --expect-version({expected})과"
            " 다릅니다 — 재서명하지 않습니다"
        )
    if latest_tag != f"v{expected}":
        raise ReleaseError(
            f"가장 최근 publish된 정식 릴리스는 {latest_tag}인데 재서명 대상은 v{expected}입니다"
            " — 예전 게시본의 재서명(replay)일 수 있어 중단합니다"
        )


# ---------------------------------------------------------------------------
# 2-1) 시계 확인 · 현재 게시본 기준 검사(M-3)
# ---------------------------------------------------------------------------


def fetch_server_date(url: str) -> datetime:
    """HTTPS 응답의 `Date` 헤더 시각(UTC). HTTP 오류 응답이어도 Date가 있으면 쓴다."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "emailtomcp-sign"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SEC) as resp:  # noqa: S310
            header = resp.headers.get("Date")
    except urllib.error.HTTPError as exc:
        header = exc.headers.get("Date") if exc.headers else None
    if not header:
        raise ValueError("Date 헤더 없음")
    parsed = email.utils.parsedate_to_datetime(header)
    if parsed.tzinfo is None:
        raise ValueError("Date 헤더에 시간대 없음")
    return parsed.astimezone(UTC)


def check_clock(
    local_now: datetime,
    *,
    fetch_date: Callable[[str], datetime],
    sources: Sequence[str] = CLOCK_SOURCES,
    max_skew: timedelta = MAX_CLOCK_SKEW,
) -> datetime:
    """로컬 시계를 서버 시각과 비교한다. max_skew를 넘거나 서버 시각을 못 얻으면 중단."""
    errors: list[str] = []
    for url in sources:
        try:
            server = fetch_date(url)
        except (OSError, ValueError) as exc:
            errors.append(f"{url}: {exc.__class__.__name__}")
            continue
        skew = local_now - server
        if abs(skew) > max_skew:
            raise ReleaseError(
                f"PC 시계가 서버 시각과 {int(abs(skew).total_seconds())}초 어긋납니다"
                f"(로컬 {format_ts(local_now)}, {url} {format_ts(server)}, 허용"
                f" {int(max_skew.total_seconds())}초) — 시계를 맞춘 뒤 다시 실행하세요"
            )
        return server
    raise ReleaseError(
        "서버 시각을 확인할 수 없어 PC 시계를 검증하지 못했습니다(네트워크 확인): "
        + "; ".join(errors)
    )


def fetch_url(url: str) -> bytes | None:
    """URL 본문(최대 64KB+1). 404면 None, 그 밖의 오류는 예외."""
    request = urllib.request.Request(url, headers={"User-Agent": "emailtomcp-sign"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SEC) as resp:  # noqa: S310
            return resp.read(MAX_MANIFEST_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def check_against_published(
    prepared: PreparedManifest,
    published: bytes | None,
    published_signature: bytes | None,
    *,
    now: datetime,
    trusted_keys: Sequence[TrustedKey],
) -> PreparedManifest:
    """현재 게시본(서명 검증 통과 시)보다 시각·버전이 뒤로 가지 않는지 확인한다(M-3).

    게시본이 없으면(stable.json과 .minisig 둘 다 404, 최초 릴리스) 그대로 돌려준다.
    게시본이 **있는데도** 서명 검증이 안 되면(변조·손상 가능성) 경고만 남기고 넘어가지
    않고 **중단한다** — 그렇지 않으면 비밀키 없이 Pages의 stable.json/.minisig만
    손상시켜도 매번 이 M-3 검사 전체(시계·다운그레이드 방지)를 우회할 수 있다(보안검토).
    둘 중 하나만 404(짝이 안 맞음)인 경우도 같은 이유로 **중단한다** — stable.json만
    지워 최초 릴리스로 가장하거나, .minisig만 지워 서명 검증을 피하는 경로를 막는다
    (L-A, Spinoza 보안검토). 통과하면 previous_issued_at에 게시본 issued_at을 넣어
    서명 후 자체검증(verify_manifest)도 같은 롤백 기준으로 확인하게 한다.
    """
    if published is None and published_signature is None:
        print(
            "[sign_release] 현재 게시된 stable.json이 없습니다"
            " — 게시본 비교를 건너뜁니다(최초 릴리스)"
        )
        return prepared
    if published is None or published_signature is None:
        missing = "stable.json" if published is None else "stable.json.minisig"
        raise ReleaseError(
            f"게시된 매니페스트와 서명 파일의 짝이 맞지 않습니다({missing}만 404) —"
            " Pages 상태를 조사하세요(서명하지 않습니다)"
        )
    try:
        sig, _ = verify_minisign(published, published_signature, tuple(trusted_keys))
        current = validate_manifest_bytes(published)
        if sig.trusted_comment != trusted_comment_for(current):
            raise ReleaseError("trusted comment 불일치")
    except (PermanentError, SecurityError, ReleaseError) as exc:
        raise ReleaseError(
            "게시된 매니페스트의 서명을 확인할 수 없습니다 — Pages가 변조됐거나 손상됐을 수"
            f" 있으니 먼저 조사하세요(서명하지 않습니다): {exc}"
        ) from exc
    if now <= current.issued_at:
        raise ReleaseError(
            f"현재 시각({format_ts(now)})이 게시본 issued_at({format_ts(current.issued_at)})보다"
            " 늦지 않습니다 — 클라이언트가 롤백으로 거부합니다(PC 시계 확인)"
        )
    new_v = parse_version(prepared.manifest.latest.version)
    cur_v = parse_version(current.latest.version)
    if new_v is None or cur_v is None or new_v < cur_v:
        raise ReleaseError(
            f"새 버전({prepared.manifest.latest.version})이"
            f" 게시본 버전({current.latest.version})보다"
            " 낮습니다 — 서명하지 않습니다"
        )
    print(
        f"[sign_release] 게시본 비교 통과(게시본 {current.latest.version},"
        f" issued_at {format_ts(current.issued_at)})"
    )
    return dataclasses.replace(prepared, previous_issued_at=current.issued_at)


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


def new_run_dir(base: Path, version: str, now: datetime) -> Path:
    """실행마다 새 결과 폴더 `<base>/<버전>-<UTC시각>/`를 만든다(L-1). 이미 있으면 중단."""
    run_dir = base / f"{version}-{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ReleaseError(f"결과 폴더가 이미 있습니다(덮어쓰지 않음): {run_dir}") from exc
    return run_dir


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
    """비밀키 파일이 있는지, git 작업트리 안에 있지 않은지 확인한다(경로는 출력하지 않음).

    이 저장소뿐 아니라 키 경로의 상위 폴더 어디에든 `.git`(폴더 또는 worktree용 파일)이
    있으면 거부한다(L-2: 다른 clone, Pages 소스 체크아웃, 상위 폴더 git 트리 포함).
    """
    if not key.is_file():
        raise ReleaseError("--key로 지정한 비밀키 파일이 없습니다")
    resolved = key.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        raise ReleaseError(
            "비밀키 파일이 저장소 작업트리 안에 있습니다 — 실수로 커밋될 수 있으니"
            " 오프라인 USB 등 저장소 밖에서 지정하세요"
        )
    for parent in resolved.parents:
        if (parent / ".git").exists():
            raise ReleaseError(
                "비밀키 파일이 git 작업트리 안에 있습니다(상위 폴더에 .git 있음) — 실수로 커밋될"
                " 수 있으니 오프라인 USB 등 git 저장소 밖에서 지정하세요"
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _summary(prepared: PreparedManifest) -> str:
    m = prepared.manifest
    notes = m.latest.notes_summary or "(비어 있음)"
    lines = [
        f"  version          : {m.latest.version}",
        f"  released_at      : {format_ts(m.latest.released_at)}",
        f"  min_version_floor: {m.min_version_floor}",
        f"  security         : {m.latest.security}",
        f"  issued_at        : {format_ts(m.issued_at)}",
        f"  expires          : {format_ts(m.expires)}",
        f"  release_page     : {m.latest.release_page}",
        f"  trusted comment  : {prepared.trusted_comment}",
        "  notes_summary    :",
    ]
    lines.extend(f"    | {line}" for line in notes.splitlines() or [""])
    lines.append("  artifacts:")
    for a in m.latest.artifacts:
        lines.append(f"    - {a.slot:<28} {a.name} ({a.size} bytes)")
        lines.append(f"      sha256 {a.sha256}")
        lines.append(f"      url    {a.url}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="오프라인 PC에서 stable.json에 minisign 서명한다(CI에서 실행 금지)."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--candidate", help="CI가 만든 manifest-candidate.json")
    mode.add_argument("--resign", help="재서명할 기존 stable.json(§14.7-5)")
    parser.add_argument(
        "--expect-version",
        required=True,
        help="서명할 릴리스 버전(X.Y.Z, 사람이 직접 입력). 후보/기존 매니페스트·태그와 같아야 한다",
    )
    parser.add_argument(
        "--artifacts-dir", help="draft 릴리스에서 내려받은 산출물 폴더(--candidate)"
    )
    parser.add_argument("--resign-signature", help="기존 서명 파일(기본: <--resign 경로>.minisig)")
    parser.add_argument("--key", required=True, help="minisign 비밀키 파일(오프라인 USB)")
    parser.add_argument("--minisign", help="minisign 실행 파일 경로(기본: PATH에서 찾기)")
    parser.add_argument("--gh", help="GitHub CLI(gh) 실행 파일 경로(기본: PATH에서 찾기)")
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help=(
            f"결과 상위 폴더(기본 {DEFAULT_OUT_DIR})."
            " 실행마다 <버전>-<시각> 하위 폴더를 새로 만든다"
        ),
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_VALID_DAYS, help="유효기간 일수(기본 30)"
    )
    parser.add_argument("--yes", action="store_true", help="서명 전 확인 질문을 건너뛴다")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    now = utc_now()
    try:
        # 인자 조합 오류는 네트워크·키 확인보다 먼저 알린다.
        if args.candidate and not args.artifacts_dir:
            raise ReleaseError("--candidate에는 --artifacts-dir가 필요합니다")
        trusted_keys = embedded_trusted_keys()
        require_pinned_trusted_keys(trusted_keys)
        expect_version = require_release_version(args.expect_version)
        key = Path(args.key)
        check_key_location(key)
        minisign = resolve_minisign(args.minisign)
        gh = resolve_gh(args.gh)
        check_clock(now, fetch_date=fetch_server_date)
        print("[sign_release] PC 시계 확인 통과")

        if args.candidate:
            candidate_path = Path(args.candidate)
            artifacts_dir = Path(args.artifacts_dir)
            prepared = prepare_from_candidate(
                candidate_path.read_bytes(), artifacts_dir, now=now, days=args.days
            )
            check_expected_version(prepared.manifest, expect_version)
            print(f"[sign_release] 버전/태그 일치 확인 통과(v{expect_version})")
            verify_attestations(
                [candidate_path]
                + [artifacts_dir / a.name for a in prepared.manifest.latest.artifacts],
                expect_version,
                gh=gh,
                runner=run_command,
            )
            print("[sign_release] 산출물 sha256/크기 대조 + 빌드 증명 확인 통과")
            prepared = check_against_published(
                prepared,
                fetch_url(PUBLISHED_MANIFEST_URL),
                fetch_url(PUBLISHED_SIGNATURE_URL),
                now=now,
                trusted_keys=trusted_keys,
            )
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
            latest_tag = latest_published_stable_tag(gh=gh, runner=run_command)
            check_resign_baseline(prepared.manifest, expect_version, latest_tag)
            print(
                "[sign_release] 기존 매니페스트 서명 검증 +"
                f" 최신 게시 릴리스({latest_tag}) 대조 통과(재서명 모드)"
            )

        print("[sign_release] 서명할 매니페스트:")
        print(_summary(prepared))
        if not args.yes:
            answer = input("위 내용으로 서명할까요? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("[sign_release] 취소했습니다.")
                return 1

        run_dir = new_run_dir(Path(args.out_dir), prepared.manifest.latest.version, now)
        manifest_path, signature_path, label = sign_and_write(
            prepared,
            run_dir,
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
        "  1. 위 폴더의 두 파일을 **한 쌍으로** GitHub Pages 소스의 manifest/ 폴더에 복사하세요"
        " (게시 주소: https://yuseungil-a11y.github.io/emailtomcp/manifest/).\n"
        "  2. 커밋/push는 Newton(vcs-manager)에게 요청하세요.\n"
        "  3. 신규 릴리스라면 draft 릴리스를 publish하세요(packaging/release/README.md)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
