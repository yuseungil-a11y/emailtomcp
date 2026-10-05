"""서명 매니페스트 스키마 (DESIGN.md §14.4).

`stable.json`의 구조를 pydantic strict 모델로 고정한다. 이 모듈은 **서명 검증을 통과한
바이트만** 파싱해야 한다(verify-then-parse, `update.verifier` 2단계 → 3단계 순서).

필드 규칙(§14.4)
- `version`, `min_version_floor`: PEP 440 정규형(`X.Y.Z` / `X.Y.ZrcN`)만. `+local`, `.devN` 금지.
- `url`, `release_page`: 빌드에 내장한 고정 접두사 `RELEASES_URL_PREFIX`로 시작해야 한다.
  단순 `startswith`만으로는 `..`/퍼센트 인코딩/사용자정보(`@`) 우회가 가능하므로 URL을
  분해해서 scheme·host·path를 각각 검사한다.
- 알 수 없는 필드는 거부한다(`extra="forbid"`).
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from emailtomcp.update.version import is_canonical_release

APP_ID = "EmailToMCP"
CHANNEL_STABLE = "stable"
SCHEMA_VERSION = 1

# 빌드에 내장한 릴리스 URL 고정 접두사(§14.4, D-7 확정 저장소).
RELEASES_URL_PREFIX = "https://github.com/yuseungil-a11y/emailtomcp/releases/"
_ALLOWED_HOST = "github.com"
_ALLOWED_PATH_PREFIX = "/yuseungil-a11y/emailtomcp/releases/"

# 산출물 1개 크기 상한(2 GiB). U2 다운로더는 매니페스트의 `size`를 다시 상한으로 쓴다.
MAX_ARTIFACT_SIZE = 2 * 1024 * 1024 * 1024
MAX_ARTIFACTS = 32
MAX_NOTES_SUMMARY = 2000

_SHA256_RE = r"^[0-9a-f]{64}$"
# 파일명: 경로 구분자·상위경로·제어문자 없는 평범한 이름만.
_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$"
_ARCH_RE = r"^[a-z0-9_]{1,16}$"
_URL_FORBIDDEN_CHARS = re.compile(r"[\s\\%@?#\x00-\x1f\x7f]")

ArtifactKind = Literal["setup", "velopack_full", "velopack_delta", "velopack_feed", "app_zip"]
Platform = Literal["windows", "macos"]


def is_allowed_release_url(url: object) -> bool:
    """URL이 허용 접두사(`RELEASES_URL_PREFIX`) 안에 있는지 엄격하게 검사한다.

    UI가 시스템 브라우저로 열기 직전에도 이 함수로 한 번 더 확인한다(방어 심층화).
    """
    if not isinstance(url, str) or len(url) > 500:
        return False
    if not url.startswith(RELEASES_URL_PREFIX):
        return False
    if _URL_FORBIDDEN_CHARS.search(url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.netloc != _ALLOWED_HOST:
        return False
    if parts.query or parts.fragment:
        return False
    path = parts.path
    if not path.startswith(_ALLOWED_PATH_PREFIX):
        return False
    segments = path.split("/")
    return not any(seg in (".", "..") for seg in segments)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Artifact(_StrictModel):
    platform: Platform
    arch: StrictStr = Field(pattern=_ARCH_RE)
    kind: ArtifactKind
    name: StrictStr = Field(pattern=_NAME_RE)
    url: StrictStr
    size: StrictInt = Field(ge=0, le=MAX_ARTIFACT_SIZE)
    sha256: StrictStr = Field(pattern=_SHA256_RE)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if not is_allowed_release_url(value):
            raise ValueError("허용된 릴리스 URL 접두사 밖입니다")
        return value

    @property
    def slot(self) -> str:
        """같은 버전 안에서 산출물을 식별하는 키(platform/arch/kind) — seen_hashes 대조용."""
        return f"{self.platform}/{self.arch}/{self.kind}"


class LatestRelease(_StrictModel):
    version: StrictStr
    released_at: AwareDatetime
    security: StrictBool
    release_page: StrictStr
    notes_summary: StrictStr = Field(max_length=MAX_NOTES_SUMMARY)
    artifacts: list[Artifact] = Field(min_length=1, max_length=MAX_ARTIFACTS)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if not is_canonical_release(value):
            raise ValueError("version은 PEP 440 정규형(X.Y.Z 또는 X.Y.ZrcN)이어야 합니다")
        return value

    @field_validator("release_page")
    @classmethod
    def _check_release_page(cls, value: str) -> str:
        if not is_allowed_release_url(value):
            raise ValueError("release_page가 허용된 릴리스 URL 접두사 밖입니다")
        return value

    @model_validator(mode="after")
    def _unique_slots(self) -> LatestRelease:
        slots = [a.slot for a in self.artifacts]
        if len(slots) != len(set(slots)):
            raise ValueError("같은 platform/arch/kind 산출물이 중복됩니다")
        return self


class Manifest(_StrictModel):
    schema_: Literal[1] = Field(alias="schema")
    app_id: StrictStr = Field(max_length=64)
    channel: StrictStr = Field(max_length=32)
    issued_at: AwareDatetime
    expires: AwareDatetime
    min_version_floor: StrictStr
    latest: LatestRelease

    @field_validator("min_version_floor")
    @classmethod
    def _check_floor(cls, value: str) -> str:
        if not is_canonical_release(value):
            raise ValueError("min_version_floor는 PEP 440 정규형이어야 합니다")
        return value

    @model_validator(mode="after")
    def _check_times(self) -> Manifest:
        if self.expires <= self.issued_at:
            raise ValueError("expires는 issued_at보다 뒤여야 합니다")
        return self

    def artifact_hashes(self) -> dict[str, dict[str, str]]:
        """seen_hashes에 저장할 형태: {slot: {"name": ..., "sha256": ...}}."""
        return {a.slot: {"name": a.name, "sha256": a.sha256} for a in self.latest.artifacts}


def parse_manifest(data: bytes) -> Manifest:
    """서명 검증을 통과한 바이트를 strict 파싱한다. 실패하면 `pydantic.ValidationError`."""
    return Manifest.model_validate_json(data, strict=True)
