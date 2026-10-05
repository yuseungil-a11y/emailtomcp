"""내장 공개키와 minisign(Ed25519) 서명 검증 (DESIGN.md §14.2, §14.4, §14.7).

운영 키: 2026-10-05 활성 K1(key_id 205BD649DF53346C) 공개키 내장 완료.
2026-10-05 사용자 결정(D-8): 예비 K2(standby) 없이 K1 단일키로 운영한다. 키 분실/탈취 시에는
추후 필요하면 신규 키 내장 + 앱 업데이트로 대응한다. 키 생성·보관·서명 절차는 §14.7을 따른다.

왜 테스트 키를 운영 상수에 넣지 않는가(중요)
- 저장소는 **공개**다(§0.1). 테스트용 키쌍의 비밀키는 `tests/fixtures/releases/`에 있어
  누구나 볼 수 있으므로, 테스트 공개키를 운영 빌드에 내장하면 **누구나 매니페스트를 위조할
  수 있는 뒷문**이 된다. 그래서 운영 상수에는 운영 키만 넣고, 테스트는 `trusted_keys` 인자로
  테스트 키를 주입한다(DI). 비어 있는 동안 앱은 업데이트 확인을 "확인 불가(서명키 미설정)"로
  **닫힌 상태(fail-closed)**로 둔다 — 네트워크 요청도 하지 않는다.
- `tests/unit/test_update_keys.py`가 "테스트 공개키가 운영 상수에 들어가지 않았는지"를 검사한다.

minisign 형식(최소 구현)
- 공개키 줄: base64(`"Ed"` 2바이트 + key_id 8바이트 + Ed25519 공개키 32바이트)
- 서명 파일 4줄:
  1. `untrusted comment: ...` (검증 대상 아님)
  2. base64(알고리즘 2바이트 + key_id 8바이트 + 서명 64바이트)
     - `ED`(기본, prehash): 서명 대상 = BLAKE2b-512(메시지)
     - `Ed`(레거시): 서명 대상 = 메시지 원문
  3. `trusted comment: <텍스트>`
  4. base64(global signature 64바이트) — 서명 대상 = (2줄의 서명 64바이트 + 텍스트)
- 두 서명(본 서명, global signature)을 **모두** 검증한다. trusted comment는 global
  signature로만 보호되므로, 이를 빼먹으면 trusted comment 위조가 가능하다.

오류 분류(보안검토 M-1, 2026-10-05 보정) — 경보(`SecurityError`)는 좁게 쓴다
- `SecurityError`(경보 + 자동 확인 정지): key_id가 신뢰 키와 **일치하는데** Ed25519 검증
  (본 서명 또는 global signature)이 실패한 경우뿐이다.
- `SignatureUnverifiable`("확인 불가", 경보 아님): 서명 파일 형식 오류, UTF-8 디코드 실패,
  base64/길이/알고리즘 오류, 신뢰 키 목록이 빈 경우.
- `UnknownSigningKey`(`SignatureUnverifiable`의 하위, "확인 불가"): 내장되지 않은 key_id.
  키 회전(§14.7-6) 뒤 구버전 클라이언트가 새 키를 모르는 정상 상황이므로 경보로 올리면
  회전 때마다 모든 구버전 사용자에게 가짜 경보가 뜬다. 사내 TLS 검사 장비가 차단 페이지를
  200으로 돌려주는 경우도 형식 오류로 떨어져 경보가 나지 않는다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from emailtomcp.core.error_codes import ErrorCode
from emailtomcp.core.errors import PermanentError, SecurityError

KeyRole = Literal["active", "standby"]

# ---------------------------------------------------------------------------
# 운영 공개키 목록(2026-10-05 K1 내장). 공개키만 둔다 — 비밀키는 저장소·CI에 절대 두지 않는다.
# 형식: (라벨, 역할, minisign 공개키 base64 줄). 활성 키 최소 1개(출시 게이트 조건).
#   K1(active) key_id=205BD649DF53346C
#   예비 키(standby)는 2026-10-05 사용자 결정(D-8)으로 두지 않는다 — standby 역할 지원
#   자체는 남겨두므로 나중에 필요하면 이 목록에 추가만 하면 된다.
# ---------------------------------------------------------------------------
EMBEDDED_PUBLIC_KEYS: tuple[tuple[str, KeyRole, str], ...] = (
    ("K1", "active", "RWRsNFPfSdZbIOJCGCtFny5oT+uyBqtw+zTK/B8Ufs/GFX/Fi2M2K5nC"),
)

_PK_ALG = b"Ed"
_SIG_ALG_PREHASHED = b"ED"
_SIG_ALG_LEGACY = b"Ed"
_UNTRUSTED_PREFIX = "untrusted comment: "
_TRUSTED_PREFIX = "trusted comment: "
_MAX_TRUSTED_COMMENT = 1024

UNKNOWN_KEY_REASON = "이 버전이 오래되어 새 서명키를 모릅니다 — 수동 업데이트가 필요합니다"


class SignatureUnverifiable(PermanentError):
    """서명을 검증할 수 없다(형식 오류·키 없음 등). 보안 경보가 아니라 "확인 불가"로 다룬다."""


class UnknownSigningKey(SignatureUnverifiable):
    """서명 key_id가 내장 키 중에 없다(키 회전 후 구버전 등) — 수동 업데이트 안내 대상."""


@dataclass(frozen=True, slots=True)
class TrustedKey:
    """내장(또는 주입) 공개키 1개."""

    label: str
    role: KeyRole
    key_id: bytes  # 8바이트(minisign 원본 바이트 순서)
    public_key: Ed25519PublicKey

    @property
    def key_id_hex(self) -> str:
        """minisign이 표시하는 형식(리틀엔디언 → 대문자 16진수)."""
        return self.key_id[::-1].hex().upper()


@dataclass(frozen=True, slots=True)
class MinisignSignature:
    algorithm: bytes
    key_id: bytes
    signature: bytes
    trusted_comment: str
    global_signature: bytes


def _b64decode_strict(text: str, error: type[Exception] = SignatureUnverifiable) -> bytes:
    try:
        return base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise error("서명/공개키 base64 형식이 올바르지 않습니다") from exc


def parse_public_key(text: str, *, label: str = "", role: KeyRole = "active") -> TrustedKey:
    """minisign 공개키(파일 전체 또는 base64 한 줄)를 파싱한다."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if lines and lines[0].startswith(_UNTRUSTED_PREFIX):
        lines = lines[1:]
    if len(lines) != 1:
        raise SecurityError("minisign 공개키 형식이 올바르지 않습니다")
    raw = _b64decode_strict(lines[0], SecurityError)
    if len(raw) != 42 or raw[:2] != _PK_ALG:
        raise SecurityError("minisign 공개키 길이/알고리즘이 올바르지 않습니다")
    return TrustedKey(
        label=label,
        role=role,
        key_id=raw[2:10],
        public_key=Ed25519PublicKey.from_public_bytes(raw[10:42]),
    )


def embedded_trusted_keys() -> tuple[TrustedKey, ...]:
    """운영 빌드에 내장된 공개키 목록. 비어 있으면 업데이트 확인은 fail-closed."""
    return tuple(
        parse_public_key(b64, label=label, role=role) for label, role, b64 in EMBEDDED_PUBLIC_KEYS
    )


def parse_signature(data: bytes) -> MinisignSignature:
    """minisign 서명 파일을 파싱한다. 형식 오류는 `SignatureUnverifiable`(경보 아님)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SignatureUnverifiable("서명 파일이 UTF-8이 아닙니다") from exc
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) != 4:
        raise SignatureUnverifiable("서명 파일은 정확히 4줄이어야 합니다")
    untrusted, sig_line, trusted_line, global_line = lines
    if not untrusted.startswith(_UNTRUSTED_PREFIX):
        raise SignatureUnverifiable("서명 파일 1줄(untrusted comment)이 올바르지 않습니다")
    if not trusted_line.startswith(_TRUSTED_PREFIX):
        raise SignatureUnverifiable("서명 파일 3줄(trusted comment)이 올바르지 않습니다")
    trusted_comment = trusted_line[len(_TRUSTED_PREFIX) :]
    if len(trusted_comment) > _MAX_TRUSTED_COMMENT:
        raise SignatureUnverifiable("trusted comment가 너무 깁니다")

    raw_sig = _b64decode_strict(sig_line)
    if len(raw_sig) != 74:
        raise SignatureUnverifiable("서명 길이가 올바르지 않습니다")
    algorithm = raw_sig[:2]
    if algorithm not in (_SIG_ALG_PREHASHED, _SIG_ALG_LEGACY):
        raise SignatureUnverifiable("지원하지 않는 서명 알고리즘입니다")
    global_sig = _b64decode_strict(global_line)
    if len(global_sig) != 64:
        raise SignatureUnverifiable("global signature 길이가 올바르지 않습니다")
    return MinisignSignature(
        algorithm=algorithm,
        key_id=raw_sig[2:10],
        signature=raw_sig[10:74],
        trusted_comment=trusted_comment,
        global_signature=global_sig,
    )


def verify_minisign(
    message: bytes, signature_file: bytes, trusted_keys: tuple[TrustedKey, ...] | list[TrustedKey]
) -> tuple[MinisignSignature, TrustedKey]:
    """메시지와 서명 파일을 검증한다. 성공하면 (파싱된 서명, 사용한 키)를 돌려준다.

    Raises:
        SecurityError: key_id가 신뢰 키와 일치하는데 본 서명 또는 global signature의
            Ed25519 검증이 실패했다(경보 대상).
        UnknownSigningKey: 내장되지 않은 key_id(확인 불가, 경보 아님).
        SignatureUnverifiable: 형식 오류, 신뢰 키 없음(확인 불가, 경보 아님).
    """
    if not trusted_keys:
        raise SignatureUnverifiable("내장된 서명 검증 키가 없습니다")
    sig = parse_signature(signature_file)
    key = next((k for k in trusted_keys if k.key_id == sig.key_id), None)
    if key is None:
        raise UnknownSigningKey(UNKNOWN_KEY_REASON)

    signed_payload = (
        hashlib.blake2b(message, digest_size=64).digest()
        if sig.algorithm == _SIG_ALG_PREHASHED
        else message
    )
    try:
        key.public_key.verify(sig.signature, signed_payload)
    except InvalidSignature as exc:
        raise SecurityError(
            "매니페스트 서명 검증에 실패했습니다", error_code=ErrorCode.UPDATE_SIGNATURE_INVALID
        ) from exc
    try:
        key.public_key.verify(
            sig.global_signature, sig.signature + sig.trusted_comment.encode("utf-8")
        )
    except InvalidSignature as exc:
        raise SecurityError(
            "trusted comment 서명(global signature) 검증에 실패했습니다",
            error_code=ErrorCode.UPDATE_SIGNATURE_INVALID,
        ) from exc
    return sig, key
