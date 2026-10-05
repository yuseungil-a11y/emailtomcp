"""매니페스트 검증 — DESIGN.md §14.4 "클라이언트 검증 순서" 11단계 구현.

하나라도 실패하면 **그 단계에서 중단**한다. 단계 번호는 주석 `[n]`으로 표시했다.

  [1] 크기 상한(64KB) 안에서 바이트 수신 — 다운로드 자체는 release_client가 상한을 걸고,
      여기서도 길이를 다시 확인한다(주입된 HttpClient가 상한을 지키지 않아도 막히도록).
  [2] 내장 키(key_id 매칭, 활성 또는 예비)로 서명 검증 — **파싱 전에** 한다(verify-then-parse).
      서명 파일 크기 상한은 매니페스트와 별도로 2KB다(M-2).
  [3] pydantic strict 파싱.
  [4] app_id/channel 일치(+ trusted comment와 JSON 값 일치, stable 채널의 rc 금지).
  [5] issued_at이 now+24h보다 미래면 거부(시계 공격).
  [6] issued_at이 저장된 last_issued_at보다 과거면 거부(롤백).
  [7] expires가 과거면 "만료 — 확인 불가"(알림·설치 모두 안 함). 이후 단계는 진행하지 않는다.
  [8] URL 접두사 검사(모델 검증과 별개로 한 번 더 — 방어 심층화).
  [9] 같은 버전인데 seen_hashes와 sha256이 다르면 SecurityError.
  [10] 상태 저장(last_issued_at, max_seen_version, seen_hashes 최근 20개).
  [11] 판정(§15.3).

오류 분류(§4.4, 보안검토 M-1로 2026-10-05 경보 대상을 좁힘)
- `SecurityError`(경보 + 업데이트 자동 확인 정지)는 다음 두 경우뿐이다.
  1. [2] key_id가 내장 키와 일치하는데 Ed25519 검증(본 서명 또는 global signature)이 실패.
  2. [9] 같은 버전인데 다른 sha256(해시 충돌).
- 그 밖의 거부 → `ManifestRejected`(PermanentError) — "확인 불가"로 표시하고 상태는 바꾸지
  않는다. 서명 파일 형식 오류·UTF-8 오류·**내장되지 않은 key_id**(키 회전 후 구버전,
  사내 TLS 검사 장비의 차단 페이지 등)도 여기에 속한다(2단계 거부).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import ValidationError

from emailtomcp.core.errors import PermanentError, SecurityError
from emailtomcp.update.keys import SignatureUnverifiable, TrustedKey, verify_minisign
from emailtomcp.update.manifest import (
    APP_ID,
    CHANNEL_STABLE,
    Manifest,
    is_allowed_release_url,
    parse_manifest,
)
from emailtomcp.update.state import TrustState, merge_seen_hashes
from emailtomcp.update.version import parse_version

MAX_MANIFEST_BYTES = 64 * 1024
# minisign 서명 파일은 4줄(trusted comment 최대 1KB 포함)이라 2KB면 충분하다(보안검토 M-2).
MAX_SIGNATURE_BYTES = 2 * 1024
FUTURE_SKEW = timedelta(hours=24)


class ManifestRejected(PermanentError):
    """검증 단계에서 매니페스트를 거부했다(보안 경보 수준은 아님). `step`은 실패한 단계 번호."""

    def __init__(self, step: int, reason: str) -> None:
        super().__init__(reason)
        self.step = step
        self.reason = reason


class Decision(StrEnum):
    AVAILABLE = "available"  # latest > current → 알림
    UP_TO_DATE = "up_to_date"  # latest == current
    IGNORED = "ignored"  # latest < current 또는 latest < max_seen_version(다운그레이드 거부)
    BELOW_FLOOR = "below_floor"  # current < min_version_floor
    EXPIRED = "expired"  # [7] 만료 — 확인 불가


@dataclass(frozen=True, slots=True)
class VerifyContext:
    current_version: str
    now: datetime
    trust: TrustState
    trusted_keys: Sequence[TrustedKey]
    app_id: str = APP_ID
    channel: str = CHANNEL_STABLE
    # dev 빌드 수동 확인: floor 연동을 하지 않는다(§14.4 dev 빌드 규칙).
    apply_floor: bool = True


@dataclass(frozen=True, slots=True)
class VerifyResult:
    decision: Decision
    manifest: Manifest
    key_label: str
    # BELOW_FLOOR일 때도 새 버전이 있는지 UI가 알 수 있게 따로 둔다.
    newer_available: bool


def _parse_trusted_comment(comment: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in comment.split():
        key, sep, value = token.partition("=")
        if not sep or not key or key in fields:
            raise ManifestRejected(4, "trusted comment 형식이 올바르지 않습니다")
        fields[key] = value
    return fields


def _check_trusted_comment(comment: str, manifest: Manifest) -> None:
    """trusted comment(`app= channel= version= issued=`)와 JSON 값이 서로 맞는지 본다."""
    fields = _parse_trusted_comment(comment)
    if fields.get("app") != manifest.app_id:
        raise ManifestRejected(4, "trusted comment의 app이 매니페스트와 다릅니다")
    if fields.get("channel") != manifest.channel:
        raise ManifestRejected(4, "trusted comment의 channel이 매니페스트와 다릅니다")
    if fields.get("version") != manifest.latest.version:
        raise ManifestRejected(4, "trusted comment의 version이 매니페스트와 다릅니다")
    issued_raw = fields.get("issued", "")
    try:
        issued = datetime.fromisoformat(issued_raw)
    except ValueError as exc:
        raise ManifestRejected(4, "trusted comment의 issued 형식이 올바르지 않습니다") from exc
    if issued.tzinfo is None or issued != manifest.issued_at:
        raise ManifestRejected(4, "trusted comment의 issued가 매니페스트와 다릅니다")


def verify_manifest(
    manifest_bytes: bytes,
    signature_bytes: bytes,
    ctx: VerifyContext,
    *,
    save_trust: Callable[[TrustState], None] | None = None,
) -> VerifyResult:
    """11단계 검증을 수행한다. `save_trust`가 None이면 [10] 저장을 건너뛴다(dev 빌드 등).

    Raises:
        SecurityError: 내장 키(key_id 일치)의 서명 검증 실패, 같은 버전 다른 해시.
        ManifestRejected: 그 밖의 거부(서명 형식 오류·미내장 key_id 포함, 2단계).
    """
    current = parse_version(ctx.current_version)
    if current is None:
        raise ManifestRejected(11, "현재 앱 버전을 해석할 수 없습니다")

    # [1] 크기 상한
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ManifestRejected(1, "매니페스트가 크기 상한(64KB)을 넘습니다")
    if len(signature_bytes) > MAX_SIGNATURE_BYTES:
        raise ManifestRejected(1, "서명 파일이 크기 상한(2KB)을 넘습니다")
    if not signature_bytes:
        raise ManifestRejected(2, "서명 파일이 없습니다")

    # [2] 서명 검증(파싱 전에)
    # 형식 오류·미내장 key_id는 "확인 불가"(2단계 거부). 내장 키의 암호학적 실패만
    # SecurityError로 그대로 올라간다(M-1).
    try:
        sig, key = verify_minisign(manifest_bytes, signature_bytes, tuple(ctx.trusted_keys))
    except SignatureUnverifiable as exc:
        raise ManifestRejected(2, str(exc)) from exc

    # [3] strict 파싱
    try:
        manifest = parse_manifest(manifest_bytes)
    except ValidationError as exc:
        raise ManifestRejected(3, f"매니페스트 형식 오류: {exc.error_count()}건") from exc

    # [4] app_id / channel / trusted comment
    if manifest.app_id != ctx.app_id:
        raise ManifestRejected(4, "app_id가 일치하지 않습니다")
    if manifest.channel != ctx.channel:
        raise ManifestRejected(4, "channel이 일치하지 않습니다")
    if ctx.channel == CHANNEL_STABLE:
        latest_v = parse_version(manifest.latest.version)
        floor_v = parse_version(manifest.min_version_floor)
        if latest_v is None or latest_v.is_prerelease:
            raise ManifestRejected(4, "stable 채널 매니페스트에 사전 릴리스(rc) 버전이 있습니다")
        if floor_v is None or floor_v.is_prerelease:
            raise ManifestRejected(4, "stable 채널 매니페스트의 floor가 사전 릴리스입니다")
    _check_trusted_comment(sig.trusted_comment, manifest)

    # [5] 미래 issued_at(시계 공격)
    if manifest.issued_at > ctx.now + FUTURE_SKEW:
        raise ManifestRejected(5, "issued_at이 현재 시각보다 24시간 넘게 미래입니다")

    # [6] 롤백(저장된 issued_at보다 과거)
    last = ctx.trust.last_issued_at
    if last is not None and manifest.issued_at < last:
        raise ManifestRejected(6, "이전에 받은 매니페스트보다 오래된 매니페스트입니다(롤백)")

    # [7] 만료 → 확인 불가(이후 단계 진행 안 함, 상태 저장도 안 함)
    if manifest.expires < ctx.now:
        return VerifyResult(
            decision=Decision.EXPIRED, manifest=manifest, key_label=key.label, newer_available=False
        )

    # [8] URL 접두사(모델 validator와 별개로 재확인)
    urls = [manifest.latest.release_page, *(a.url for a in manifest.latest.artifacts)]
    if not all(is_allowed_release_url(u) for u in urls):
        raise ManifestRejected(8, "허용된 릴리스 URL 접두사 밖의 URL이 있습니다")

    # [9] 같은 버전, 다른 해시
    version = manifest.latest.version
    new_slots = manifest.artifact_hashes()
    seen_slots = ctx.trust.seen_hashes.get(version, {})
    for slot, entry in new_slots.items():
        previous = seen_slots.get(slot)
        if previous is not None and previous != entry:
            raise SecurityError(
                f"같은 버전({version})의 산출물({slot}) 해시가 이전과 다릅니다"
                " — 업데이트를 정지합니다"
            )

    # [10] 상태 저장
    latest = parse_version(version)
    floor = parse_version(manifest.min_version_floor)
    assert latest is not None and floor is not None  # [3] 모델 검증으로 보장됨
    old_max = parse_version(ctx.trust.max_seen_version) if ctx.trust.max_seen_version else None
    new_max = latest if old_max is None or latest > old_max else old_max
    if save_trust is not None:
        save_trust(
            TrustState(
                last_issued_at=manifest.issued_at,
                max_seen_version=str(new_max),
                seen_hashes=merge_seen_hashes(ctx.trust.seen_hashes, version, new_slots),
            )
        )

    # [11] 판정
    newer = latest > current and (old_max is None or latest >= old_max)
    if ctx.apply_floor and current < floor:
        decision = Decision.BELOW_FLOOR
    elif latest < current or (old_max is not None and latest < old_max):
        decision = Decision.IGNORED
    elif latest == current:
        decision = Decision.UP_TO_DATE
    else:
        decision = Decision.AVAILABLE
    return VerifyResult(
        decision=decision, manifest=manifest, key_label=key.label, newer_available=newer
    )
