"""UpdateChecker 통합 — fake GitHub Pages 서버 + 실제 HTTP 클라이언트 + 실제 DB(settings).

§14.6 P1 매트릭스 중 네트워크/상태/이벤트가 얽힌 케이스를 끝까지(end-to-end) 확인한다.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fakes"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from _helpers import make_db  # noqa: E402
from fake_github_releases import (  # noqa: E402
    TEST_KEY_STANDBY,
    TEST_KEY_UNTRUSTED,
    FakeGithubReleases,
    build_manifest,
    sign_manifest,
    test_trusted_keys,
)

from emailtomcp.core.clock import FakeClock  # noqa: E402
from emailtomcp.core.events import EventBus  # noqa: E402
from emailtomcp.update.checker import StatusKind, UpdateChecker, UpdateStatus  # noqa: E402
from emailtomcp.update.release_client import HttpxReleaseClient  # noqa: E402
from emailtomcp.update.state import UpdateStateStore  # noqa: E402

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 15, tzinfo=UTC)


@pytest.fixture
def fake():
    with FakeGithubReleases() as server:
        yield server


@pytest.fixture
def db(tmp_path):
    database = make_db(tmp_path)
    yield database
    database.stop()


class _Recorder:
    def __init__(self, bus: EventBus) -> None:
        self.events: list[tuple[str, object]] = []
        bus.subscribe("*", lambda name, payload: self.events.append((name, payload)))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]


def _checker(fake, db, *, current: str = "1.0.0", keys=None, clock=None, base_url=None):  # noqa: ANN001, ANN202
    bus = EventBus()
    recorder = _Recorder(bus)
    checker = UpdateChecker(
        http_client=HttpxReleaseClient(base_url or fake.base_url),
        state_store=UpdateStateStore(db),
        event_bus=bus,
        clock=clock or FakeClock(NOW),
        current_version=current,
        trusted_keys=test_trusted_keys() if keys is None else keys,
        request_timeout=5.0,
    )
    return checker, recorder


def test_m1_new_version_available_publishes_event_and_saves_state(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest(version="1.0.1")))
    checker, rec = _checker(fake, db)
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.AVAILABLE
    assert status.latest_version == "1.0.1"
    assert status.release_page.startswith("https://github.com/yuseungil-a11y/emailtomcp/releases/")
    assert "UpdateAvailable" in rec.names() and "UpdateStatusChanged" in rec.names()
    trust = UpdateStateStore(db).load_trust()
    assert trust.max_seen_version == "1.0.1"
    assert trust.last_issued_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert "1.0.1" in trust.seen_hashes
    # 저장된 스냅샷을 다시 읽어도 같다
    assert checker.last_status().kind == StatusKind.AVAILABLE


def test_m2_up_to_date(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest(version="1.0.1")))
    checker, rec = _checker(fake, db, current="1.0.1")
    assert checker.check_once(manual=True).kind == StatusKind.UP_TO_DATE
    assert "UpdateAvailable" not in rec.names()


def test_m8_tampered_signature_raises_alert_and_halts_auto_check(fake, db) -> None:
    data, sig = sign_manifest(build_manifest())
    fake.publish(data.replace(b"1.0.1", b"6.6.6"), sig)
    checker, rec = _checker(fake, db)
    status = checker.check_once(manual=False)
    assert status.kind == StatusKind.SECURITY_ALERT
    assert status.halted
    assert "SecurityAlert" in rec.names()
    assert "UpdateAvailable" not in rec.names()
    # 신뢰 상태는 바뀌지 않는다
    trust = UpdateStateStore(db).load_trust()
    assert trust.last_issued_at is None and trust.max_seen_version is None
    # 자동 확인은 정지 — 네트워크 요청도 하지 않는다
    before = len(fake.request_log)
    assert checker.run_scheduled(force=True) is None
    assert len(fake.request_log) == before
    # 서버가 정상화된 뒤 수동 확인이 성공하면 정지가 풀린다
    fake.publish(*sign_manifest(build_manifest()))
    recovered = checker.check_once(manual=True)
    assert recovered.kind == StatusKind.AVAILABLE and not recovered.halted


def test_m9_untrusted_key(fake, db) -> None:
    """미내장 key_id(키 회전 후 구버전 등)는 경보·정지가 아니라 "확인 불가" + 수동 업데이트 안내.

    보안검토 M-1.
    """
    fake.publish(*sign_manifest(build_manifest(), TEST_KEY_UNTRUSTED))
    checker, rec = _checker(fake, db)
    status = checker.check_once(manual=False)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert not status.halted
    assert "새 서명키를 모릅니다" in status.reason and "수동 업데이트" in status.reason
    assert "SecurityAlert" not in rec.names()
    assert UpdateStateStore(db).load_trust().last_issued_at is None
    # 자동 확인은 정지되지 않는다
    before = len(fake.request_log)
    assert checker.run_scheduled(force=True) is not None
    assert len(fake.request_log) > before


def test_html_block_page_is_unverifiable_not_alert(fake, db) -> None:
    """M-1 시나리오: 사내 SSL 검사 장비가 차단 HTML을 200으로 돌려줘도 경보가 나지 않는다."""
    html = b"<html><body>Blocked by corporate proxy</body></html>"
    fake.publish(html, html)
    checker, rec = _checker(fake, db)
    status = checker.check_once(manual=False)
    assert status.kind == StatusKind.UNVERIFIABLE and not status.halted
    assert "SecurityAlert" not in rec.names()


def test_oversized_signature_rejected_by_client(fake, db) -> None:
    """서명 파일 전용 상한 2KB(M-2) — 클라이언트 스트리밍 단계에서 차단."""
    data, sig = sign_manifest(build_manifest())
    fake.publish(data, sig + b" " * 4096)
    checker, _ = _checker(fake, db)
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert "2KB" in status.reason


def test_slow_drip_server_hits_total_timeout(fake, db) -> None:
    """M-2: 조금씩 느리게 보내는 서버는 벽시계 전체 상한에서 끊고 "확인 불가"(네트워크)로 처리."""
    data, sig = sign_manifest(build_manifest())
    fake.publish(data, sig)
    fake.set_drip(0.05)  # 1바이트/50ms — 읽기 1회 타임아웃(5초)에는 절대 안 걸린다
    bus = EventBus()
    checker = UpdateChecker(
        http_client=HttpxReleaseClient(fake.base_url, total_timeout=0.5),
        state_store=UpdateStateStore(db),
        event_bus=bus,
        clock=FakeClock(NOW),
        current_version="1.0.0",
        trusted_keys=test_trusted_keys(),
        request_timeout=5.0,
    )
    started = time.monotonic()
    status = checker.check_once(manual=True)
    elapsed = time.monotonic() - started
    assert status.kind == StatusKind.UNVERIFIABLE
    assert "느려" in status.reason
    assert elapsed < 5.0


def test_m10_signature_file_missing(fake, db) -> None:
    data, _sig = sign_manifest(build_manifest())
    fake.publish(data, None)
    checker, rec = _checker(fake, db)
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert "서명 파일 없음" in status.reason
    assert "UpdateAvailable" not in rec.names()
    assert UpdateStateStore(db).load_trust().last_issued_at is None


def test_m11_expired_manifest_unverifiable(fake, db) -> None:
    manifest = build_manifest(issued_at="2026-09-01T00:00:00Z", expires="2026-10-01T00:00:00Z")
    fake.publish(*sign_manifest(manifest))
    checker, rec = _checker(fake, db)
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert "만료" in status.reason
    assert "UpdateAvailable" not in rec.names()


def test_m12_rollback_after_newer_manifest(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest(version="1.0.2", issued_at="2026-11-10T00:00:00Z")))
    checker, _ = _checker(fake, db)
    assert checker.check_once(manual=True).kind == StatusKind.AVAILABLE
    # 정상 서명이지만 더 오래된 매니페스트(재생 공격)
    fake.publish(*sign_manifest(build_manifest(version="1.0.1", issued_at="2026-11-01T00:00:00Z")))
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert "롤백" in status.reason
    # 이전에 검증된 정보는 남는다
    assert status.latest_version == "1.0.2"
    assert UpdateStateStore(db).load_trust().max_seen_version == "1.0.2"


def test_m14_hash_collision_security_alert(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest(version="1.0.1")))
    checker, _ = _checker(fake, db)
    checker.check_once(manual=True)
    forged = build_manifest(version="1.0.1", issued_at="2026-11-05T00:00:00Z", digest_seed="x")
    fake.publish(*sign_manifest(forged))
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.SECURITY_ALERT and status.halted


def test_m16_oversized_response_rejected_by_client(fake, db) -> None:
    data, sig = sign_manifest(build_manifest())
    fake.publish(data + b" " * (70 * 1024), sig)
    checker, _ = _checker(fake, db)
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert "64KB" in status.reason


def test_m17_floor_not_met(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest(version="1.0.2", floor="1.0.1")))
    checker, rec = _checker(fake, db, current="1.0.0")
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.BELOW_FLOOR and status.below_floor
    assert "UpdateAvailable" in rec.names()
    # floor 미달은 네트워크 실패가 이어져도 유지된다(배너가 사라지지 않음)
    fake.publish(None, None)
    later = checker.check_once(manual=True)
    assert later.kind == StatusKind.UNVERIFIABLE and later.below_floor


def test_m18_dev_build_does_not_auto_check(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest(version="1.0.2", floor="1.0.1")))
    checker, _ = _checker(fake, db, current="1.0.1.dev3+gabc")
    assert checker.is_dev_build
    assert not checker.auto_check_enabled()
    assert checker.run_scheduled(force=True) is None
    assert fake.request_log == []
    # 수동 확인은 판정만 보여주고 floor 연동/신뢰 상태 저장은 하지 않는다
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.AVAILABLE
    assert not status.below_floor
    assert UpdateStateStore(db).load_trust().last_issued_at is None


def test_m19_network_failure_quietly_recorded(db) -> None:
    checker, rec = _checker(None, db, base_url="http://127.0.0.1:9/manifest/")
    status = checker.check_once(manual=False)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert "네트워크" in status.reason
    assert rec.names() == ["UpdateStatusChanged"]
    assert checker.last_status().kind == StatusKind.UNVERIFIABLE


def test_m20_standby_key_accepted(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest(), TEST_KEY_STANDBY))
    checker, _ = _checker(fake, db)
    assert checker.check_once(manual=True).kind == StatusKind.AVAILABLE


def test_no_embedded_keys_fails_closed_without_network(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest()))
    checker, _ = _checker(fake, db, keys=())
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert "서명 검증 키" in status.reason
    assert fake.request_log == []


def test_scheduled_check_respects_24h_interval_and_toggle(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest()))
    clock = FakeClock(NOW)
    checker, _ = _checker(fake, db, clock=clock)
    assert checker.auto_check_enabled()  # 정식 빌드 기본 on
    assert checker.run_scheduled(force=True) is not None  # 기동 30초 뒤 첫 확인
    assert checker.run_scheduled() is None  # 24시간 안 지남
    clock.advance(25 * 3600)
    assert checker.run_scheduled() is not None
    checker.set_auto_check(False)
    clock.advance(25 * 3600)
    assert checker.run_scheduled() is None


def test_stale_after_seven_days_without_success(fake, db) -> None:
    fake.publish(*sign_manifest(build_manifest()))
    clock = FakeClock(NOW)
    checker, _ = _checker(fake, db, clock=clock)
    checker.check_once(manual=True)
    assert not checker.is_stale()
    fake.publish(None, None)
    clock.advance(8 * 24 * 3600)
    checker.check_once(manual=True)
    assert checker.is_stale()


def test_status_from_dict_drops_tampered_release_page() -> None:
    status = UpdateStatus.from_dict({"kind": "available", "release_page": "https://evil.com/x"})
    assert status.release_page is None


def test_app_base_url_override_only_in_dev_build(monkeypatch) -> None:
    """§4.6: base_url 덮어쓰기(EMAILTOMCP_UPDATE_BASE_URL)는 dev 빌드에서만 받는다."""
    from emailtomcp import app as app_module

    monkeypatch.setenv(app_module.UPDATE_BASE_URL_ENV, "http://127.0.0.1:1/manifest/")
    assert app_module._resolve_update_base_url("1.0.0") == app_module.DEFAULT_BASE_URL
    assert app_module._resolve_update_base_url("1.0.1.dev3+gabc") == "http://127.0.0.1:1/manifest/"
    monkeypatch.delenv(app_module.UPDATE_BASE_URL_ENV)
    assert app_module._resolve_update_base_url("1.0.1.dev3+gabc") == app_module.DEFAULT_BASE_URL


def test_app_builds_checker_with_embedded_keys_fail_closed(db, monkeypatch) -> None:
    """내장 키가 비면 실제 조립도 네트워크 없이 '확인 불가'로 닫힌다(fail-closed 유지)."""
    from emailtomcp import app as app_module
    from emailtomcp.update import keys as keys_module

    monkeypatch.setattr(keys_module, "EMBEDDED_PUBLIC_KEYS", ())
    checker = app_module._build_update_checker(db, EventBus(), FakeClock(NOW), "1.0.0")
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert "서명 검증 키" in status.reason


def test_app_builds_checker_with_production_keys_rejects_test_key(fake, db, monkeypatch) -> None:
    """운영 K1 내장(2026-10-05) 후 실제 조립: 테스트 키 서명은 미내장 키(확인 불가)로 거부된다.

    실제 GitHub Pages로 나가지 않도록 클라이언트 base_url만 가짜 서버로 바꾼다.
    """
    from emailtomcp import app as app_module
    from emailtomcp.update.keys import UNKNOWN_KEY_REASON

    monkeypatch.setattr(
        app_module,
        "HttpxReleaseClient",
        lambda _base_url, **kw: HttpxReleaseClient(fake.base_url, **kw),
    )
    fake.publish(*sign_manifest(build_manifest(version="1.0.1")))
    checker = app_module._build_update_checker(db, EventBus(), FakeClock(NOW), "1.0.0")
    status = checker.check_once(manual=True)
    assert status.kind == StatusKind.UNVERIFIABLE
    assert status.reason == UNKNOWN_KEY_REASON
    assert not status.halted


def test_ui_api_set_setting_rejects_update_keys(db) -> None:
    """L-8: 범용 set_setting으로 업데이트 신뢰 상태 키를 쓸 수 없다(update/state.py 경유만)."""
    from emailtomcp.app import UiApi
    from emailtomcp.core.errors import PolicyError

    api = UiApi(
        db=db,
        clock=FakeClock(NOW),
        mail_blob_dir=Path("."),
        secret_store=None,  # type: ignore[arg-type] — set_setting은 쓰지 않는다
        sync_service=None,  # type: ignore[arg-type]
        send_service=None,  # type: ignore[arg-type]
        draft_service=None,  # type: ignore[arg-type]
    )
    for key in ("update.last_issued_at", "update.state", "update.auto_check"):
        with pytest.raises(PolicyError):
            asyncio.run(api.set_setting(key, "2099-01-01T00:00:00+00:00"))
    assert UpdateStateStore(db).load_trust().last_issued_at is None
    # 일반 설정 키는 그대로 쓸 수 있다
    asyncio.run(api.set_setting("theme", "dark"))
    assert asyncio.run(api.get_setting("theme")) == "dark"
