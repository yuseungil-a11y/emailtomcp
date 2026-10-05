"""update.verifier — §14.4 11단계 검증과 §14.6 P1 테스트 매트릭스(단위 수준).

매트릭스 번호는 테스트 이름/ID에 `m<번호>`로 표시한다.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))

from fake_github_releases import (  # noqa: E402
    RELEASES,
    TEST_KEY_ACTIVE,
    TEST_KEY_STANDBY,
    TEST_KEY_UNTRUSTED,
    build_manifest,
    encode_manifest,
    sign_manifest,
    test_trusted_keys,
)

from emailtomcp.core.errors import SecurityError  # noqa: E402
from emailtomcp.update.state import SEEN_HASHES_LIMIT, TrustState  # noqa: E402
from emailtomcp.update.verifier import (  # noqa: E402
    Decision,
    ManifestRejected,
    VerifyContext,
    verify_manifest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, tzinfo=UTC)


def _ctx(current: str = "1.0.0", trust: TrustState | None = None, **kw) -> VerifyContext:  # noqa: ANN003
    return VerifyContext(
        current_version=current,
        now=kw.pop("now", NOW),
        trust=trust or TrustState(),
        trusted_keys=test_trusted_keys(),
        **kw,
    )


class _Saver:
    def __init__(self) -> None:
        self.saved: list[TrustState] = []

    def __call__(self, trust: TrustState) -> None:
        self.saved.append(trust)


def _verify(manifest: dict, current: str = "1.0.0", trust: TrustState | None = None, **kw):  # noqa: ANN003, ANN201
    data, sig = sign_manifest(manifest)
    saver = _Saver()
    result = verify_manifest(data, sig, _ctx(current, trust, **kw), save_trust=saver)
    return result, saver


# ---------------------------------------------------------------- 판정(§14.6 #1~#7)


@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [
        ("1.0.0", "1.0.1", Decision.AVAILABLE),  # m1
        ("1.0.1", "1.0.1", Decision.UP_TO_DATE),  # m2
        ("1.1.0", "1.0.9", Decision.IGNORED),  # m3 다운그레이드
        ("1.0.9", "1.0.10", Decision.AVAILABLE),  # m4 문자열 비교 함정
        ("1.0.10", "1.1.0", Decision.AVAILABLE),  # m5
        ("2.0.0rc1", "2.0.0", Decision.AVAILABLE),  # m7 rc 테스트 빌드
    ],
    ids=["m1", "m2", "m3", "m4", "m5", "m7"],
)
def test_decision_matrix(current: str, latest: str, expected: Decision) -> None:
    result, saver = _verify(build_manifest(version=latest), current=current)
    assert result.decision == expected
    assert len(saver.saved) == 1
    assert saver.saved[0].max_seen_version == latest


def test_m6_rc_in_stable_channel_rejected() -> None:
    with pytest.raises(ManifestRejected) as exc_info:
        _verify(build_manifest(version="2.0.0rc1"), current="1.1.0")
    assert exc_info.value.step == 4


def test_ignored_when_lower_than_max_seen_version() -> None:
    """서버가 이전에 본 최고 버전보다 낮은 latest를 내밀면 무시(local floor, §14.5)."""
    trust = TrustState(max_seen_version="1.0.5")
    result, saver = _verify(build_manifest(version="1.0.3"), current="1.0.0", trust=trust)
    assert result.decision == Decision.IGNORED
    assert saver.saved[0].max_seen_version == "1.0.5"  # 낮아지지 않는다


# ---------------------------------------------------------------- 서명(§14.6 #8~#10, #20)


def test_m8_signature_byte_flip_rejected_and_state_unchanged() -> None:
    data, sig = sign_manifest(build_manifest())
    tampered = bytearray(data)
    tampered[10] ^= 0x01  # 매니페스트 1바이트 변조
    saver = _Saver()
    with pytest.raises(SecurityError):
        verify_manifest(bytes(tampered), sig, _ctx(), save_trust=saver)
    assert saver.saved == []


def test_m9_untrusted_key_rejected() -> None:
    """미내장 key_id는 경보가 아니라 2단계 거부("확인 불가", 보안검토 M-1)."""
    data, sig = sign_manifest(build_manifest(), TEST_KEY_UNTRUSTED)
    saver = _Saver()
    with pytest.raises(ManifestRejected) as exc_info:
        verify_manifest(data, sig, _ctx(), save_trust=saver)
    assert exc_info.value.step == 2
    assert "새 서명키를 모릅니다" in exc_info.value.reason
    assert saver.saved == []


def test_rogue_key_with_trusted_key_id_is_security_error() -> None:
    """key_id는 내장 키와 같은데 서명이 맞지 않으면 경보(SecurityError) 유지(M-1 경보 조건 1)."""
    data = encode_manifest(build_manifest())
    sig = TEST_KEY_UNTRUSTED.sign(data, "x=y", key_id=TEST_KEY_ACTIVE.key_id)
    saver = _Saver()
    with pytest.raises(SecurityError):
        verify_manifest(data, sig, _ctx(), save_trust=saver)
    assert saver.saved == []


@pytest.mark.parametrize(
    "bad_sig",
    [
        b"<html>blocked</html>",
        b"\xff\xfe\x00",
        b"untrusted comment: x\n!!!\ntrusted comment: y\nzz\n",
    ],
    ids=["html", "not-utf8", "bad-base64"],
)
def test_malformed_signature_is_rejected_not_alert(bad_sig: bytes) -> None:
    """서명 파일 형식 오류·UTF-8 오류는 2단계 거부(경보 아님, M-1)."""
    data = encode_manifest(build_manifest())
    with pytest.raises(ManifestRejected) as exc_info:
        verify_manifest(data, bad_sig, _ctx())
    assert exc_info.value.step == 2


def test_oversized_signature_file_rejected_at_step1() -> None:
    """서명 파일 상한은 매니페스트(64KB)와 별도로 2KB(M-2)."""
    data, sig = sign_manifest(build_manifest())
    with pytest.raises(ManifestRejected) as exc_info:
        verify_manifest(data, sig + b" " * 2048, _ctx())
    assert exc_info.value.step == 1


def test_m10_missing_signature_rejected() -> None:
    data = encode_manifest(build_manifest())
    saver = _Saver()
    with pytest.raises(ManifestRejected) as exc_info:
        verify_manifest(data, b"", _ctx(), save_trust=saver)
    assert exc_info.value.step == 2
    assert saver.saved == []


def test_m20_standby_key_accepted() -> None:
    data, sig = sign_manifest(build_manifest(), TEST_KEY_STANDBY)
    result = verify_manifest(data, sig, _ctx(), save_trust=None)
    assert result.decision == Decision.AVAILABLE
    assert result.key_label == "K2-test"


def test_legacy_ed_signature_accepted() -> None:
    data, sig = sign_manifest(build_manifest(), TEST_KEY_ACTIVE, prehash=False)
    assert verify_manifest(data, sig, _ctx()).decision == Decision.AVAILABLE


def test_verify_happens_before_parse() -> None:
    """서명 없는 쓰레기 JSON은 파싱 오류(3단계)가 아니라 서명 단계(2단계)에서 막힌다."""
    garbage = b'{"schema": "not-a-number"'
    # 미내장 키 → 2단계 거부(M-1: 경보 아님)
    with pytest.raises(ManifestRejected) as exc_info:
        verify_manifest(garbage, TEST_KEY_UNTRUSTED.sign(garbage, "x=y"), _ctx())
    assert exc_info.value.step == 2
    # 내장 key_id를 사칭한 위조 서명 → 2단계에서 경보(파싱까지 가지 않음)
    forged = TEST_KEY_UNTRUSTED.sign(garbage, "x=y", key_id=TEST_KEY_ACTIVE.key_id)
    with pytest.raises(SecurityError):
        verify_manifest(garbage, forged, _ctx())


def test_trusted_comment_mismatch_rejected() -> None:
    manifest = build_manifest(version="1.0.1")
    comment = "app=EmailToMCP channel=stable version=1.0.2 issued=2026-11-01T00:00:00Z"
    data, sig = sign_manifest(manifest, trusted_comment=comment)
    with pytest.raises(ManifestRejected) as exc_info:
        verify_manifest(data, sig, _ctx())
    assert exc_info.value.step == 4


# ---------------------------------------------------------------- 시각(§14.6 #11~#13)


def test_m11_expired_is_unverifiable_without_state_change() -> None:
    manifest = build_manifest(issued_at="2026-09-01T00:00:00Z", expires="2026-10-01T00:00:00Z")
    result, saver = _verify(manifest)
    assert result.decision == Decision.EXPIRED
    assert saver.saved == []


def test_m12_issued_at_older_than_stored_rejected() -> None:
    trust = TrustState(last_issued_at=datetime(2026, 11, 10, tzinfo=UTC))
    with pytest.raises(ManifestRejected) as exc_info:
        _verify(build_manifest(issued_at="2026-11-01T00:00:00Z"), trust=trust)
    assert exc_info.value.step == 6


def test_same_issued_at_is_accepted() -> None:
    trust = TrustState(last_issued_at=datetime(2026, 11, 1, tzinfo=UTC))
    result, _ = _verify(build_manifest(issued_at="2026-11-01T00:00:00Z"), trust=trust)
    assert result.decision == Decision.AVAILABLE


def test_m13_issued_at_two_days_in_future_rejected() -> None:
    manifest = build_manifest(issued_at="2026-11-17T00:00:00Z", expires="2026-12-17T00:00:00Z")
    with pytest.raises(ManifestRejected) as exc_info:
        _verify(manifest)
    assert exc_info.value.step == 5


def test_issued_at_within_24h_skew_accepted() -> None:
    manifest = build_manifest(issued_at="2026-11-15T20:00:00Z", expires="2026-12-15T00:00:00Z")
    assert _verify(manifest)[0].decision == Decision.AVAILABLE


# ---------------------------------------------------------------- 해시/URL/크기(§14.6 #14~#16)


def test_m14_same_version_different_sha256_is_security_error() -> None:
    first, saver = _verify(build_manifest(version="1.0.1"))
    assert first.decision == Decision.AVAILABLE
    trust = saver.saved[0]
    forged = build_manifest(version="1.0.1", issued_at="2026-11-02T00:00:00Z", digest_seed="evil")
    saver2 = _Saver()
    data, sig = sign_manifest(forged)
    with pytest.raises(SecurityError, match="해시"):
        verify_manifest(data, sig, _ctx(trust=trust), save_trust=saver2)
    assert saver2.saved == []


def test_same_version_same_hash_resign_accepted() -> None:
    """30일 재서명(§14.7-5): issued_at/expires만 바뀐 같은 내용은 정상 수락."""
    _, saver = _verify(build_manifest(version="1.0.1"))
    resigned = build_manifest(
        version="1.0.1", issued_at="2026-11-10T00:00:00Z", expires="2026-12-10T00:00:00Z"
    )
    result, _ = _verify(resigned, trust=saver.saved[0])
    assert result.decision == Decision.AVAILABLE


@pytest.mark.parametrize(
    "page",
    [
        "https://evil.example.com/yuseungil-a11y/emailtomcp/releases/tag/v1.0.1",
        "https://github.com/someone/emailtomcp/releases/tag/v1.0.1",
        "http://github.com/yuseungil-a11y/emailtomcp/releases/tag/v1.0.1",
        RELEASES + "../../../evil/releases/tag/v1.0.1",
        RELEASES + "%2e%2e/%2e%2e/evil",
        "https://github.com/yuseungil-a11y/emailtomcp/releases/@evil.com",
        "https://github.com@evil.com/yuseungil-a11y/emailtomcp/releases/x",
        RELEASES + "tag/v1.0.1?redirect=https://evil.com",
    ],
)
def test_m15_release_page_outside_prefix_rejected(page: str) -> None:
    with pytest.raises(ManifestRejected) as exc_info:
        _verify(build_manifest(release_page=page))
    assert exc_info.value.step == 3  # 모델 validator에서 먼저 걸린다(8단계는 재확인)


def test_m16_oversized_manifest_rejected() -> None:
    manifest = build_manifest()
    manifest["latest"]["notes_summary"] = "x" * 1000
    data, sig = sign_manifest(manifest)
    oversized = data + b" " * (64 * 1024)
    with pytest.raises(ManifestRejected) as exc_info:
        verify_manifest(oversized, sig, _ctx())
    assert exc_info.value.step == 1


# ---------------------------------------------------------------- floor(§14.6 #17)


def test_m17_below_floor() -> None:
    result, _ = _verify(build_manifest(version="1.0.2", floor="1.0.1"), current="1.0.0")
    assert result.decision == Decision.BELOW_FLOOR
    assert result.newer_available


def test_dev_build_manual_check_skips_floor() -> None:
    """§14.4 dev 빌드: 수동 확인은 판정만 하고 floor 연동은 하지 않는다."""
    result, _ = _verify(
        build_manifest(version="1.0.2", floor="1.0.1"), current="1.0.0.dev3+gabc", apply_floor=False
    )
    assert result.decision == Decision.AVAILABLE


# ---------------------------------------------------------------- 스키마/기타


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(app_id="OtherApp"),
        lambda m: m.update(channel="beta"),
    ],
    ids=["app_id", "channel"],
)
def test_app_id_or_channel_mismatch_rejected(mutate) -> None:  # noqa: ANN001
    manifest = build_manifest()
    mutate(manifest)
    with pytest.raises(ManifestRejected) as exc_info:
        _verify(manifest)
    assert exc_info.value.step == 4


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(schema=2),
        lambda m: m.update(unexpected="x"),
        lambda m: m["latest"].update(version="1.0.1+local"),
        lambda m: m["latest"].update(version="1.0.1.dev1"),
        lambda m: m.update(min_version_floor="v1.0.0"),
        lambda m: m["latest"].update(security="true"),
        lambda m: m["latest"]["artifacts"][0].update(size="1024"),
        lambda m: m["latest"]["artifacts"][0].update(sha256="ABC"),
        lambda m: m["latest"]["artifacts"][0].update(name="../evil.exe"),
        lambda m: m["latest"]["artifacts"][0].update(url="https://evil.com/x.exe"),
        lambda m: m["latest"].update(artifacts=[]),
        lambda m: m.update(issued_at="2026-11-01T00:00:00"),  # 타임존 없음
        lambda m: m.update(expires="2026-10-01T00:00:00Z"),  # issued 이전
    ],
)
def test_schema_violations_rejected_at_step3(mutate) -> None:  # noqa: ANN001
    manifest = build_manifest()
    mutate(manifest)
    with pytest.raises(ManifestRejected) as exc_info:
        _verify(manifest)
    assert exc_info.value.step == 3


def test_unparseable_current_version_rejected() -> None:
    with pytest.raises(ManifestRejected):
        _verify(build_manifest(), current="unknown")


def test_seen_hashes_keep_only_recent_20_versions() -> None:
    trust = TrustState()
    for patch in range(25):
        version = f"1.0.{patch + 1}"
        day = patch + 1
        result, saver = _verify(
            build_manifest(
                version=version,
                issued_at=f"2026-10-{day:02d}T00:00:00Z",
                expires="2026-12-31T00:00:00Z",
            ),
            trust=trust,
        )
        assert result.decision == Decision.AVAILABLE
        trust = saver.saved[0]
    assert len(trust.seen_hashes) == SEEN_HASHES_LIMIT
    assert "1.0.25" in trust.seen_hashes
    assert "1.0.1" not in trust.seen_hashes
    assert trust.max_seen_version == "1.0.25"
