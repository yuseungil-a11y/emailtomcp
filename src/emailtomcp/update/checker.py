"""업데이트 "확인 1회 실행" (DESIGN.md §3.4, §14.4, §11.7~§11.8).

`UpdateChecker.check_once()`가 다운로드 → 11단계 검증(`update.verifier`) → 상태 저장 →
이벤트 발행을 한 번 수행한다. 블로킹 함수이므로 Backend의 executor에서 호출한다.

스케줄(§3.4): 기동 30초 뒤 1회, 이후 24시간마다(Backend 주기 작업이 1시간 간격으로
`run_scheduled()`를 부르고, 마지막 확인이 24시간 지났을 때만 실제로 확인한다 — PC 절전으로
단조 시계가 멈춰도 벽시계 기준으로 다시 확인하게 하려는 것). 수동 확인은 설정 화면의 [지금 확인].

dev 빌드(버전에 `.dev`나 `+`, 또는 버전 미상): 자동 확인 기본 off. 수동 확인은 판정만
보여주고 floor 연동·신뢰 상태 저장은 하지 않는다(§14.4).

발행 이벤트(core.events, §4.2)
- `UpdateStatusChanged`: 확인할 때마다(payload = `UpdateStatus.to_dict()`)
- `UpdateAvailable`: 새 버전이 있을 때(available, 또는 floor 미달이면서 새 버전 있음)
- `SecurityAlert`: 내장 키의 서명 검증 실패·해시 충돌(경보 1회 + 업데이트 자동 확인 정지,
  §11.8). 서명 형식 오류·미내장 key_id는 경보가 아니라 "확인 불가"다(보안검토 M-1).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from emailtomcp.core.errors import PermanentError, SecurityError, TransientError
from emailtomcp.update.keys import TrustedKey, UnknownSigningKey, embedded_trusted_keys
from emailtomcp.update.manifest import is_allowed_release_url
from emailtomcp.update.release_client import (
    MANIFEST_PATH,
    SIGNATURE_PATH,
    HttpResponse,
    ResponseTooLarge,
)
from emailtomcp.update.state import UpdateStateStore
from emailtomcp.update.verifier import (
    Decision,
    ManifestRejected,
    VerifyContext,
    verify_manifest,
)
from emailtomcp.update.version import is_dev_build, parse_version

if TYPE_CHECKING:
    from emailtomcp.core.clock import Clock
    from emailtomcp.core.events import EventBus
    from emailtomcp.core.ports import HttpClient

logger = logging.getLogger(__name__)

EVENT_UPDATE_AVAILABLE = "UpdateAvailable"
EVENT_UPDATE_STATUS_CHANGED = "UpdateStatusChanged"
EVENT_SECURITY_ALERT = "SecurityAlert"

AUTO_CHECK_INTERVAL = timedelta(hours=24)
STARTUP_DELAY_SEC = 30.0
SCHEDULER_TICK_SEC = 3600.0
STALE_WARNING_AFTER = timedelta(days=7)


class StatusKind(StrEnum):
    NEVER = "never"  # 아직 확인한 적 없음
    UP_TO_DATE = "up_to_date"
    IGNORED = "ignored"  # 서버 버전이 더 낮음(다운그레이드 거부) — UI는 '최신'과 같게 표시
    AVAILABLE = "available"
    BELOW_FLOOR = "below_floor"
    UNVERIFIABLE = "unverifiable"  # 네트워크/만료/거부/키 미설정 — "확인 불가"
    SECURITY_ALERT = "security_alert"


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """UI 표시용 스냅샷. `update.state`에 dict로 저장된다."""

    kind: str = StatusKind.NEVER.value
    current_version: str = ""
    is_dev_build: bool = False
    checked_at: str | None = None
    last_success_at: str | None = None
    reason: str = ""
    # 마지막으로 **검증을 통과한** 매니페스트의 값(확인 불가가 이어져도 유지)
    latest_version: str | None = None
    released_at: str | None = None
    security: bool = False
    release_page: str | None = None
    notes_summary: str = ""
    floor_version: str | None = None
    below_floor: bool = False
    newer_available: bool = False
    # 보안 경보로 자동 확인이 정지됐는지. 수동 확인이 성공하면 해제된다.
    halted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> UpdateStatus:
        if not isinstance(data, dict):
            return cls()
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in data.items() if k in known}
        try:
            status = cls(**clean)
        except TypeError:
            return cls()
        # 저장된 링크도 쓰기 전에 다시 검증한다(DB 변조 방어).
        if status.release_page is not None and not is_allowed_release_url(status.release_page):
            status = replace(status, release_page=None)
        return status


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_below_floor(current: str, floor: str | None, *, apply_floor: bool) -> bool:
    if not apply_floor or floor is None:
        return False
    cur, fl = parse_version(current), parse_version(floor)
    return cur is not None and fl is not None and cur < fl


class UpdateChecker:
    """확인 1회 실행 + 자동 확인 판단. 스레드 안전(동시 호출은 직렬화)."""

    def __init__(
        self,
        *,
        http_client: HttpClient,
        state_store: UpdateStateStore,
        event_bus: EventBus | None,
        clock: Clock,
        current_version: str,
        trusted_keys: Sequence[TrustedKey] | None = None,
        manifest_path: str = MANIFEST_PATH,
        signature_path: str = SIGNATURE_PATH,
        request_timeout: float = 10.0,
    ) -> None:
        self._http = http_client
        self._store = state_store
        self._bus = event_bus
        self._clock = clock
        self._current_version = current_version
        self._trusted_keys = (
            tuple(trusted_keys) if trusted_keys is not None else embedded_trusted_keys()
        )
        self._manifest_path = manifest_path
        self._signature_path = signature_path
        self._timeout = request_timeout
        self._dev_build = is_dev_build(current_version)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 조회
    @property
    def current_version(self) -> str:
        return self._current_version

    @property
    def is_dev_build(self) -> bool:
        return self._dev_build

    def auto_check_enabled(self) -> bool:
        """저장값이 없으면 정식 빌드는 on, dev 빌드는 off(§11.7)."""
        return self._store.get_auto_check(default=not self._dev_build)

    def set_auto_check(self, enabled: bool) -> None:
        self._store.set_auto_check(enabled)

    def last_status(self) -> UpdateStatus:
        status = UpdateStatus.from_dict(self._store.load_status())
        # 현재 실행 중인 버전 기준으로 다시 계산한다(업데이트 후 첫 기동 등).
        return replace(
            status,
            current_version=self._current_version,
            is_dev_build=self._dev_build,
            below_floor=_is_below_floor(
                self._current_version, status.floor_version, apply_floor=not self._dev_build
            ),
        )

    def is_stale(self) -> bool:
        """마지막 성공 확인이 7일을 넘었는지(§11.8 '7일 넘게 확인하지 못하면 배너')."""
        status = self.last_status()
        if status.kind == StatusKind.NEVER.value:
            return False
        last_ok = _parse_iso(status.last_success_at)
        checked = _parse_iso(status.checked_at)
        reference = last_ok or checked
        return reference is not None and self._clock.now() - reference > STALE_WARNING_AFTER

    # ------------------------------------------------------------ 실행
    def run_scheduled(self, *, force: bool = False) -> UpdateStatus | None:
        """자동 확인 진입점. 꺼져 있거나, 경보로 정지됐거나, 24시간이 안 지났으면 건너뛴다.

        `force=True`는 기동 30초 뒤 첫 확인에 쓴다(24시간 조건만 무시, on/off·정지는 지킨다).
        """
        if not self.auto_check_enabled():
            return None
        previous = self.last_status()
        if previous.halted:
            logger.info("보안 경보로 업데이트 자동 확인이 정지된 상태입니다")
            return None
        if not force:
            checked = _parse_iso(previous.checked_at)
            if checked is not None and self._clock.now() - checked < AUTO_CHECK_INTERVAL:
                return None
        return self.check_once(manual=False)

    def check_once(self, *, manual: bool) -> UpdateStatus:
        """확인을 1회 수행하고 결과 상태를 저장·발행한다. 예외를 밖으로 올리지 않는다."""
        with self._lock:
            previous = self.last_status()
            now = self._clock.now()
            status = self._check_locked(previous, now, manual=manual)
            try:
                self._store.save_status(status.to_dict())
            except Exception:  # noqa: BLE001 — 상태 저장 실패가 확인 결과 전달을 막으면 안 됨
                logger.exception("업데이트 상태 저장 실패")
        self._publish(status)
        return status

    def _base_status(self, previous: UpdateStatus, now: datetime) -> UpdateStatus:
        return replace(
            previous,
            current_version=self._current_version,
            is_dev_build=self._dev_build,
            checked_at=_iso(now),
            reason="",
        )

    def _fail(self, base: UpdateStatus, reason: str) -> UpdateStatus:
        return replace(base, kind=StatusKind.UNVERIFIABLE.value, reason=reason)

    def _check_locked(self, previous: UpdateStatus, now: datetime, *, manual: bool) -> UpdateStatus:
        base = self._base_status(previous, now)
        if parse_version(self._current_version) is None:
            return self._fail(base, "앱 버전 정보를 알 수 없어 업데이트를 확인할 수 없습니다")
        if not self._trusted_keys:
            # fail-closed: 검증 키가 없으면 네트워크 요청 자체를 하지 않는다.
            return self._fail(
                base, "이 빌드에는 업데이트 서명 검증 키가 내장되지 않아 확인할 수 없습니다"
            )

        try:
            manifest_bytes = self._fetch(self._manifest_path, missing_reason="매니페스트 없음")
            signature_bytes = self._fetch(self._signature_path, missing_reason="서명 파일 없음")
            apply_floor = not self._dev_build
            ctx = VerifyContext(
                current_version=self._current_version,
                now=now,
                trust=self._store.load_trust(),
                trusted_keys=self._trusted_keys,
                apply_floor=apply_floor,
            )
            result = verify_manifest(
                manifest_bytes,
                signature_bytes,
                ctx,
                save_trust=None if self._dev_build else self._store.save_trust,
            )
        except SecurityError as exc:
            logger.error("업데이트 보안 경보: %s", exc)
            return replace(base, kind=StatusKind.SECURITY_ALERT.value, reason=str(exc), halted=True)
        except TransientError as exc:
            logger.info("업데이트 확인 실패(네트워크): %s", exc)
            return self._fail(base, f"네트워크 오류로 확인하지 못했습니다({exc})")
        except ManifestRejected as exc:
            logger.warning("업데이트 매니페스트 거부(%s단계): %s", exc.step, exc.reason)
            if isinstance(exc.__cause__, UnknownSigningKey):
                # 키 회전 후 구버전: 경보가 아니라 수동 업데이트 안내(M-1)
                return self._fail(base, exc.reason)
            return self._fail(base, f"매니페스트를 거부했습니다: {exc.reason}")
        except PermanentError as exc:
            return self._fail(base, f"확인하지 못했습니다: {exc}")
        except Exception as exc:  # noqa: BLE001 — 업데이트 확인 실패가 앱을 죽이면 안 됨
            logger.exception("업데이트 확인 중 예상치 못한 오류")
            return self._fail(base, f"확인 중 오류가 발생했습니다({type(exc).__name__})")

        if result.decision == Decision.EXPIRED:
            return self._fail(base, "매니페스트가 만료되었습니다(서버 재서명 대기) — 확인 불가")

        latest = result.manifest.latest
        verified = replace(
            base,
            last_success_at=_iso(now),
            latest_version=latest.version,
            released_at=_iso(latest.released_at),
            security=latest.security,
            release_page=latest.release_page,
            notes_summary=latest.notes_summary,
            floor_version=result.manifest.min_version_floor,
            below_floor=result.decision == Decision.BELOW_FLOOR,
            newer_available=result.newer_available,
            # 수동이든 자동이든 검증에 성공하면 경보 정지를 해제한다(자동은 정지 중 실행되지 않음).
            halted=False,
        )
        kind = {
            Decision.AVAILABLE: StatusKind.AVAILABLE,
            Decision.UP_TO_DATE: StatusKind.UP_TO_DATE,
            Decision.IGNORED: StatusKind.IGNORED,
            Decision.BELOW_FLOOR: StatusKind.BELOW_FLOOR,
        }[result.decision]
        reason = ""
        if kind == StatusKind.IGNORED:
            reason = "서버의 버전이 현재(또는 이전에 확인한) 버전보다 낮아 무시했습니다"
        if self._dev_build:
            reason = (reason + " " if reason else "") + "(개발 빌드: 판정만 표시합니다)"
        return replace(verified, kind=kind.value, reason=reason)

    def _fetch(self, path: str, *, missing_reason: str) -> bytes:
        try:
            response = self._http.get(path, timeout=self._timeout)
        except ResponseTooLarge as exc:
            if path == self._signature_path:
                raise ManifestRejected(1, "서명 파일 크기가 상한(2KB)을 넘습니다") from exc
            raise ManifestRejected(1, "매니페스트 크기가 상한(64KB)을 넘습니다") from exc
        if not isinstance(response, HttpResponse):
            # 다른 HttpClient 구현(테스트 등)도 status_code/content만 있으면 받는다.
            status_code = getattr(response, "status_code", None)
            content = getattr(response, "content", None)
        else:
            status_code, content = response.status_code, response.content
        if status_code == 404:
            if path == self._signature_path:
                raise ManifestRejected(2, missing_reason)
            raise TransientError(missing_reason)
        if status_code != 200 or not isinstance(content, bytes | bytearray):
            raise TransientError(f"HTTP {status_code}")
        return bytes(content)

    def _publish(self, status: UpdateStatus) -> None:
        if self._bus is None:
            return
        payload = status.to_dict()
        self._bus.publish(EVENT_UPDATE_STATUS_CHANGED, payload)
        if status.kind == StatusKind.SECURITY_ALERT.value:
            self._bus.publish(EVENT_SECURITY_ALERT, {"source": "update", "reason": status.reason})
        elif status.kind == StatusKind.AVAILABLE.value or (
            status.kind == StatusKind.BELOW_FLOOR.value and status.newer_available
        ):
            self._bus.publish(EVENT_UPDATE_AVAILABLE, payload)
