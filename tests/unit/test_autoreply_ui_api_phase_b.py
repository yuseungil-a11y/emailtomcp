"""UiApi P3 Phase B 경로: 규칙 CRUD·드라이런·미매칭 동작·잡 목록·큐 일시정지·CLI 고정 보호."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _autoreply_helpers import eml, enable, make_account, make_db, query, store

from emailtomcp.app import UiApi
from emailtomcp.autoreply.controller import AutoReplyController
from emailtomcp.core.clock import SystemClock
from emailtomcp.core.errors import PolicyError
from emailtomcp.storage.db import Database

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path: Path):  # noqa: ANN201
    database = make_db(tmp_path)
    yield database
    database.stop()


def _api(db: Database) -> UiApi:
    return UiApi(
        db=db,
        clock=SystemClock(),
        mail_blob_dir=db.path.parent,
        secret_store=None,  # type: ignore[arg-type]
        sync_service=None,  # type: ignore[arg-type]
        send_service=None,  # type: ignore[arg-type]
        draft_service=None,  # type: ignore[arg-type]
        autoreply_controller=AutoReplyController(db, clock=SystemClock()),
    )


RULE = {
    "name": "거래처",
    "action": "draft",
    "reply_mode": "generated",
    "conditions": {
        "match": "all",
        "conditions": [{"field": "from_domain", "op": "in_list", "value": ["client.example"]}],
    },
    "max_chars": 300,
}


def test_rule_crud_and_auto_send_rejected(db) -> None:  # noqa: ANN001
    api = _api(db)

    async def scenario() -> None:
        with pytest.raises(PolicyError, match="다음 업데이트"):
            await api.save_rule(None, {**RULE, "action": "auto_send"})
        with pytest.raises(PolicyError, match="RE2"):
            await api.save_rule(
                None,
                {
                    **RULE,
                    "conditions": {
                        "match": "all",
                        "conditions": [{"field": "subject", "op": "regex", "value": "(a)\\1"}],
                    },
                },
            )
        saved = await api.save_rule(None, RULE)
        rules = await api.list_rules()
        assert [r["name"] for r in rules] == ["거래처"] and rules[0]["version"] == 1
        updated = await api.save_rule(saved["id"], {**RULE, "name": "거래처2"}, 1)
        assert updated["version"] == 2
        with pytest.raises(PolicyError, match="다른 곳에서"):
            await api.save_rule(saved["id"], RULE, 1)  # CAS
        await api.set_rule_enabled(saved["id"], False)
        assert (await api.get_rule(saved["id"]))["enabled"] is False
        assert await api.delete_rule(saved["id"]) is True
        assert await api.list_rules() == []

    asyncio.run(scenario())


def test_dry_run_and_unmatched_action_work_while_off(db, tmp_path: Path) -> None:  # noqa: ANN001
    api = _api(db)
    account = make_account(db)
    store(db, tmp_path / "mail", account, eml())
    store(db, tmp_path / "mail", account, eml(frm="x@other.example"))

    async def scenario() -> None:
        report = await api.dry_run_rule(RULE, 50)
        assert report["total"] == 2 and report["matched"] == 1 and report["would_draft"] == 1
        await api.set_unmatched_action("draft")
        assert await api.get_unmatched_action() == "draft"
        with pytest.raises(PolicyError):
            await api.set_unmatched_action("auto_send")
        with pytest.raises(PolicyError):
            await api.set_setting("autoreply.unmatched_action", "draft")  # 범용 경로는 막힘

    asyncio.run(scenario())
    assert query(db, "SELECT COUNT(*) FROM auto_reply_jobs")[0][0] == 0


def test_claude_pin_setting_is_protected(db) -> None:  # noqa: ANN001
    api = _api(db)
    for key in ("claude.cli_pin", " Claude.CLI_PIN ", "claude.other"):
        with pytest.raises(PolicyError):
            asyncio.run(api.set_setting(key, {"program": "C:/evil.exe"}))
    with pytest.raises(PolicyError):  # 임시 폴더·없는 파일은 고정할 수 없다
        asyncio.run(api.pin_claude_cli(str(db.path.parent / "nope.exe"), None))


def test_queue_status_pause_and_job_list(db, tmp_path: Path) -> None:  # noqa: ANN001
    api = _api(db)

    async def scenario() -> None:
        with pytest.raises(PolicyError, match="꺼져"):
            await api.set_autoreply_queue_paused(True)
        status = await api.get_autoreply_queue_status()
        assert status["enabled"] is False and status["cli"]["pinned"] is False

    asyncio.run(scenario())
    enable(db)

    async def scenario_on() -> None:
        assert (await api.get_autoreply_queue_status())["queue_state"] == "running"
        assert await api.set_autoreply_queue_paused(True) == "paused_user"
        assert await api.set_autoreply_queue_paused(False) == "running"
        assert await api.list_autoreply_jobs() == []
        view = await api.get_autoreply_toggle()
        assert view["preflight"]["claude_cli_pinned"] is False

    asyncio.run(scenario_on())


def test_rule_and_account_changes_revalidate_running_job(db) -> None:  # noqa: ANN001
    """데카르트 31번 L-4: 규칙 삭제·변경·중지, 계정 비활성 커밋 뒤 러너 재확인(토큰 폐기+kill)."""
    api = _api(db)
    calls: list[str] = []

    class _Runner:
        def revalidate_current(self) -> bool:
            calls.append("revalidate")
            return False

    api.attach_job_runner(_Runner())  # type: ignore[arg-type]
    account = make_account(db)

    async def scenario() -> None:
        saved = await api.save_rule(None, RULE)
        assert calls == []  # 새 규칙은 실행 중 잡과 무관
        await api.save_rule(saved["id"], {**RULE, "name": "바뀜"}, 1)
        assert calls == ["revalidate"]
        await api.set_rule_enabled(saved["id"], True)
        assert calls == ["revalidate"]  # 켜기는 재확인 불필요
        await api.set_rule_enabled(saved["id"], False)
        assert calls == ["revalidate"] * 2
        await api.delete_rule(saved["id"])
        assert calls == ["revalidate"] * 3
        await api.update_account(account, {"signature": "x"}, None)
        assert calls == ["revalidate"] * 3
        await api.update_account(account, {"auto_reply_enabled": False}, None)
        assert calls == ["revalidate"] * 4

    asyncio.run(scenario())
