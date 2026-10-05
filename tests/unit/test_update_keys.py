"""update.keys — minisign(Ed25519) 서명 검증과 내장 키 가드."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))

from fake_github_releases import (  # noqa: E402
    TEST_KEY_ACTIVE,
    TEST_KEY_STANDBY,
    TEST_KEY_UNTRUSTED,
    test_trusted_keys,
)

from emailtomcp import __version__ as APP_VERSION  # noqa: E402
from emailtomcp.core.errors import SecurityError  # noqa: E402
from emailtomcp.update import keys  # noqa: E402
from emailtomcp.update.version import is_dev_build  # noqa: E402

pytestmark = pytest.mark.unit

MESSAGE = b'{"hello": "world"}\n'
COMMENT = "app=EmailToMCP channel=stable version=1.0.1 issued=2026-11-01T00:00:00Z"


@pytest.mark.parametrize("prehash", [True, False], ids=["ED-prehash", "Ed-legacy"])
def test_valid_signature_verifies(prehash: bool) -> None:
    sig_file = TEST_KEY_ACTIVE.sign(MESSAGE, COMMENT, prehash=prehash)
    sig, key = keys.verify_minisign(MESSAGE, sig_file, test_trusted_keys())
    assert key.label == "K1-test"
    assert sig.trusted_comment == COMMENT


def test_standby_key_accepted() -> None:
    """§14.6 #20: 예비 키로 서명해도 수락한다."""
    sig_file = TEST_KEY_STANDBY.sign(MESSAGE, COMMENT)
    _sig, key = keys.verify_minisign(MESSAGE, sig_file, test_trusted_keys())
    assert key.role == "standby"


def test_unknown_key_rejected() -> None:
    """§14.6 #9: 미내장 키로 서명하면 거부한다 — 경보(SecurityError)가 아니라 "확인 불가".

    보안검토 M-1: 키 회전 후 구버전 클라이언트가 새 키를 모르는 정상 상황이므로 경보로
    올리면 회전 때마다 가짜 경보가 뜬다. 수동 업데이트 안내 문구로 구분한다.
    """
    sig_file = TEST_KEY_UNTRUSTED.sign(MESSAGE, COMMENT)
    with pytest.raises(keys.UnknownSigningKey, match="새 서명키를 모릅니다") as exc_info:
        keys.verify_minisign(MESSAGE, sig_file, test_trusted_keys())
    assert not isinstance(exc_info.value, SecurityError)
    assert isinstance(exc_info.value, keys.SignatureUnverifiable)


def test_rogue_key_reusing_trusted_key_id_rejected() -> None:
    """공격자가 key_id만 내장 키 것으로 바꿔도 서명 자체가 맞지 않아 거부된다.

    key_id가 내장 키와 일치하는데 암호학적 검증이 실패한 경우이므로 경보(SecurityError) 유지.
    """
    sig_file = TEST_KEY_UNTRUSTED.sign(MESSAGE, COMMENT, key_id=TEST_KEY_ACTIVE.key_id)
    with pytest.raises(SecurityError):
        keys.verify_minisign(MESSAGE, sig_file, test_trusted_keys())


def test_message_tamper_rejected() -> None:
    sig_file = TEST_KEY_ACTIVE.sign(MESSAGE, COMMENT)
    with pytest.raises(SecurityError):
        keys.verify_minisign(MESSAGE.replace(b"world", b"w0rld"), sig_file, test_trusted_keys())


def test_signature_byte_flip_rejected() -> None:
    """§14.6 #8: 서명 1바이트 변조 → 거부."""
    sig_file = TEST_KEY_ACTIVE.sign(MESSAGE, COMMENT)
    lines = sig_file.decode().split("\n")
    raw = bytearray(base64.b64decode(lines[1]))
    raw[-1] ^= 0x01
    lines[1] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(SecurityError):
        keys.verify_minisign(MESSAGE, "\n".join(lines).encode(), test_trusted_keys())


def test_trusted_comment_tamper_rejected_by_global_signature() -> None:
    sig_file = TEST_KEY_ACTIVE.sign(MESSAGE, COMMENT)
    forged = sig_file.replace(b"version=1.0.1", b"version=9.9.9")
    with pytest.raises(SecurityError, match="global signature"):
        keys.verify_minisign(MESSAGE, forged, test_trusted_keys())


@pytest.mark.parametrize(
    "bad",
    [b"", b"garbage", b"untrusted comment: x\n!!!\ntrusted comment: y\nzzz\n", b"\xff\xfe"],
)
def test_malformed_signature_file_rejected(bad: bytes) -> None:
    """형식 오류·UTF-8 오류는 "확인 불가"(경보 아님, M-1) — 사내 TLS 검사 장비 차단 페이지 등."""
    with pytest.raises(keys.SignatureUnverifiable) as exc_info:
        keys.verify_minisign(MESSAGE, bad, test_trusted_keys())
    assert not isinstance(exc_info.value, SecurityError)


def test_html_block_page_is_unverifiable_not_alert() -> None:
    """M-1 시나리오: 프록시가 서명 파일 자리에 HTML 차단 페이지를 200으로 돌려준 경우."""
    html = b"<html><body>Blocked by corporate proxy</body></html>"
    with pytest.raises(keys.SignatureUnverifiable):
        keys.verify_minisign(MESSAGE, html, test_trusted_keys())


def test_no_trusted_keys_fails_closed() -> None:
    sig_file = TEST_KEY_ACTIVE.sign(MESSAGE, COMMENT)
    with pytest.raises(keys.SignatureUnverifiable):
        keys.verify_minisign(MESSAGE, sig_file, ())


def test_public_key_file_roundtrip() -> None:
    parsed = keys.parse_public_key(TEST_KEY_ACTIVE.public_key_file(), label="x")
    assert parsed.key_id == TEST_KEY_ACTIVE.key_id
    assert len(parsed.key_id_hex) == 16


def test_test_keys_are_never_embedded_in_production() -> None:
    """가드: 테스트 키(공개 저장소에 시드가 있음)가 운영 내장 키 목록에 들어가면 안 된다."""
    embedded_ids = {k.key_id for k in keys.embedded_trusted_keys()}
    for test_key in (TEST_KEY_ACTIVE, TEST_KEY_STANDBY, TEST_KEY_UNTRUSTED):
        assert test_key.key_id not in embedded_ids
    embedded_lines = {line for _l, _r, line in keys.EMBEDDED_PUBLIC_KEYS}
    assert TEST_KEY_ACTIVE.public_key_line() not in embedded_lines
    assert TEST_KEY_STANDBY.public_key_line() not in embedded_lines


def test_embedded_keys_parse_if_present() -> None:
    """운영 키가 들어오면 모두 파싱 가능해야 하고, 활성 키가 최소 1개 있어야 한다."""
    parsed = keys.embedded_trusted_keys()
    if parsed:
        assert any(k.role == "active" for k in parsed)


PRODUCTION_K1_LINE = "RWRsNFPfSdZbIOJCGCtFny5oT+uyBqtw+zTK/B8Ufs/GFX/Fi2M2K5nC"
PRODUCTION_K1_KEY_ID_HEX = "205BD649DF53346C"


def test_production_k1_is_embedded_as_active() -> None:
    """운영 K1(2026-10-05 내장)이 활성 키로 파싱되고 key_id가 minisign 표시값과 일치한다."""
    parsed = keys.embedded_trusted_keys()
    k1 = [k for k in parsed if k.key_id_hex == PRODUCTION_K1_KEY_ID_HEX]
    assert len(k1) == 1
    assert k1[0].role == "active"
    assert k1[0].label == "K1"
    assert k1[0].key_id == keys.parse_public_key(PRODUCTION_K1_LINE).key_id
    # 내장 key_id는 서로 겹치지 않아야 한다(조회가 모호해지면 안 됨).
    assert len({k.key_id for k in parsed}) == len(parsed)


def test_production_k1_key_id_is_recognized_not_unknown() -> None:
    """K1 key_id로 표시된 서명은 UnknownSigningKey가 아니라 '내장 키'로 인식된다.

    비밀키가 없으므로 진짜 서명은 만들 수 없다 — 테스트 키로 서명하고 key_id만 K1로 바꾸면
    key_id 조회는 통과하고 Ed25519 검증에서 SecurityError(경보)가 나야 한다. 이는 K1이
    신뢰 키 목록에서 실제로 조회된다는 증거다.
    """
    k1 = keys.parse_public_key(PRODUCTION_K1_LINE)
    sig_file = TEST_KEY_UNTRUSTED.sign(MESSAGE, COMMENT, key_id=k1.key_id)
    with pytest.raises(SecurityError) as exc_info:
        keys.verify_minisign(MESSAGE, sig_file, keys.embedded_trusted_keys())
    assert not isinstance(exc_info.value, keys.UnknownSigningKey)


def test_other_key_id_is_unknown_against_embedded_keys() -> None:
    """내장되지 않은 key_id는 운영 키 목록 기준으로 UnknownSigningKey(확인 불가)다."""
    sig_file = TEST_KEY_ACTIVE.sign(MESSAGE, COMMENT)
    with pytest.raises(keys.UnknownSigningKey):
        keys.verify_minisign(MESSAGE, sig_file, keys.embedded_trusted_keys())


@pytest.mark.skipif(
    is_dev_build(APP_VERSION),
    reason=f"dev 빌드({APP_VERSION})는 운영 키 내장 게이트 대상이 아님 — 정식 버전 태그 시 작동",
)
def test_release_build_has_embedded_keys() -> None:
    """출시 게이트(보안검토 H-1 수정권고 ②): 정식 빌드는 활성 운영 키가 반드시 내장돼야 한다.

    키가 빈 채로 출시하면 그 설치본은 영구히 업데이트 알림을 받지 못한다(공개키는 원격으로
    추가할 수 없음). dev 빌드(`.dev`/`+` 포함)에서는 스킵되고, v1.0.0처럼 정규 버전으로
    빌드할 때 자동으로 작동한다.
    2026-10-05 사용자 결정(D-8): 예비 키(K2, standby) 없이 K1 단일키로 운영 — 예비 키는
    요구하지 않는다. 활성 키 1개만 있으면 통과한다.
    """
    _assert_release_key_gate(keys.embedded_trusted_keys())


def _assert_release_key_gate(parsed: tuple[keys.TrustedKey, ...]) -> None:
    assert any(k.role == "active" for k in parsed), "활성(active) 운영 키가 내장되지 않았습니다"


def test_release_key_gate_passes_with_single_active_key() -> None:
    """dev 빌드에서도 항상 실행: 현재 내장 목록(K1 active 단일)이 출시 게이트를 만족한다.

    정식 버전 태그 전에 게이트 통과 여부를 미리 확인하기 위한 테스트다(standby 불필요).
    """
    parsed = keys.embedded_trusted_keys()
    _assert_release_key_gate(parsed)
    assert not any(k.role == "standby" for k in parsed)  # 현재 결정: 예비 키 미사용


def test_release_key_gate_fails_without_active_key() -> None:
    """활성 키가 없으면(빈 목록 또는 standby만) 게이트는 여전히 실패해야 한다."""
    with pytest.raises(AssertionError):
        _assert_release_key_gate(())
    with pytest.raises(AssertionError):
        _assert_release_key_gate((TEST_KEY_STANDBY.trusted_key("standby"),))
