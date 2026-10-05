"""UiApi 자동회신 토글 경로 (DESIGN.md §7.0 보호 키, §7.11 UiApi, §12.3 P3 "보호 키").

- `UiApi.set_setting`으로 보호 키 6개를 쓰면 전부 PolicyError이고 DB는 바뀌지 않는다.
- 그 밖의 `autoreply.*` 키도 화이트리스트에 없으면 거부된다(보안검토 L-4).
- 전용 API(`get_autoreply_toggle`/`enable_autoreply`/`disable_autoreply`)로만 토글이 바뀐다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _helpers import make_db

from emailtomcp.app import UiApi
from emailtomcp.autoreply.controller import AutoReplyController
from emailtomcp.core.clock import SystemClock
from emailtomcp.core.errors import PolicyError
from emailtomcp.storage.db import Database

pytestmark = pytest.mark.unit

PROTECTED = (
    "autoreply.enabled",
    "autoreply.generation",
    "autoreply.watermark_message_id",
    "autoreply.enabled_changed_at",
    "autoreply.queue_state",
    "autoreply.autosend_state",
)


@pytest.fixture
def db(tmp_path: Path):  # noqa: ANN201
    database = make_db(tmp_path)
    yield database
    database.stop()


def _api(db: Database, *, with_controller: bool = True) -> UiApi:
    controller = AutoReplyController(db, clock=SystemClock()) if with_controller else None
    # 토글 경로는 db·컨트롤러만 쓴다 — 나머지 서비스는 이 테스트에서 호출되지 않는다.
    return UiApi(
        db=db,
        clock=SystemClock(),
        mail_blob_dir=db.path.parent,
        secret_store=None,  # type: ignore[arg-type]
        sync_service=None,  # type: ignore[arg-type]
        send_service=None,  # type: ignore[arg-type]
        draft_service=None,  # type: ignore[arg-type]
        autoreply_controller=controller,
    )


def _settings_snapshot(db: Database) -> list:
    conn = db.new_read_connection()
    try:
        return [tuple(r) for r in conn.execute("SELECT key, value_json FROM settings ORDER BY key")]
    finally:
        conn.close()


@pytest.mark.parametrize("key", PROTECTED)
@pytest.mark.parametrize("value", [True, False, 99, "running", None])
def test_set_setting_rejects_every_protected_key(db: Database, key: str, value: object) -> None:
    api = _api(db)
    before = _settings_snapshot(db)
    with pytest.raises(PolicyError):
        asyncio.run(api.set_setting(key, value))
    assert _settings_snapshot(db) == before


@pytest.mark.parametrize(
    "key", [" autoreply.enabled", "autoreply.enabled ", "AutoReply.Enabled", "AUTOREPLY.GENERATION"]
)
def test_set_setting_rejects_protected_key_variants(db: Database, key: str) -> None:
    with pytest.raises(PolicyError):
        asyncio.run(_api(db).set_setting(key, True))


def test_set_setting_still_allows_ordinary_keys(db: Database) -> None:
    api = _api(db)
    asyncio.run(api.set_setting("autoreply_timeout_sec", 90))  # 접두사 'autoreply.'가 아님
    asyncio.run(api.set_setting("theme", "dark"))
    assert asyncio.run(api.get_setting("autoreply_timeout_sec")) == 90


@pytest.mark.parametrize(
    "key",
    [
        "autoreply.unmatched_action",
        "autoreply.auto_summary_enabled",
        "autoreply.timeout_sec",
        "autoreply.anything_new",
        " AutoReply.Unmatched_Action ",
        "AUTOREPLY.X",
    ],
)
def test_set_setting_rejects_any_autoreply_prefixed_key(db: Database, key: str) -> None:
    """L-4 회귀: 보호 목록에 없는 `autoreply.*` 키도 범용 경로로는 쓸 수 없다(접두사 기본 거부)."""
    api = _api(db)
    before = _settings_snapshot(db)
    with pytest.raises(PolicyError):
        asyncio.run(api.set_setting(key, "draft"))
    assert _settings_snapshot(db) == before


def test_autoreply_prefix_whitelist_allows_listed_key_but_never_protected(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """화이트리스트 키만 예외로 허용되고, 보호 키는 화이트리스트에 넣어도 거부된다."""
    from emailtomcp import app as app_module

    monkeypatch.setattr(
        app_module,
        "AUTOREPLY_GENERIC_WRITABLE_KEYS",
        frozenset({"autoreply.unmatched_action", "autoreply.enabled"}),
    )
    api = _api(db)
    asyncio.run(api.set_setting("autoreply.unmatched_action", "draft"))
    assert asyncio.run(api.get_setting("autoreply.unmatched_action")) == "draft"
    with pytest.raises(PolicyError):
        asyncio.run(api.set_setting("autoreply.enabled", True))
    with pytest.raises(PolicyError):
        asyncio.run(api.set_setting("autoreply.timeout_sec", 90))


def test_toggle_via_dedicated_api(db: Database) -> None:
    api = _api(db)

    async def _scenario() -> None:
        view = await api.get_autoreply_toggle()
        assert view["enabled"] is False
        assert view["generation"] == 1
        assert view["preflight"]["autoreply_accounts"] == 0
        assert view["preflight"]["claude_cli_pinned"] is False  # Phase B: 미고정
        assert view["preflight"]["unmatched_action"] == "ignore"

        on = await api.enable_autoreply(view["generation"])
        assert on == {"enabled": True, "generation": 2}
        assert (await api.get_autoreply_toggle())["enabled"] is True

        with pytest.raises(PolicyError):  # 옛 세대로는 다시 켤 수 없음(CAS)
            await api.enable_autoreply(view["generation"])

        off = await api.disable_autoreply()
        assert off["enabled"] is False
        assert off["generation"] == 3
        assert (off["cancelled_jobs"], off["reverted_outbox"], off["sending_in_flight"]) == (
            0,
            0,
            0,
        )

    asyncio.run(_scenario())


def test_toggle_api_without_controller_fails_closed(db: Database) -> None:
    api = _api(db, with_controller=False)
    with pytest.raises(Exception, match="자동회신 컨트롤러"):
        asyncio.run(api.enable_autoreply(1))
    with pytest.raises(Exception, match="자동회신 컨트롤러"):
        asyncio.run(api.disable_autoreply())
    assert asyncio.run(api.get_autoreply_toggle())["enabled"] is False
