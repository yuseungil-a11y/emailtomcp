"""RuleEngine — 수신 메일 평가와 잡 생성 (DESIGN.md §7.2, §7.9 G2·G3) — P3 Phase B.

평가 순서(§7.2)
0. 전역 토글: off이면 MessageReceived를 **구독하지 않는다**(AutoReplyController가 start/stop).
   평가 중에도 메모리 세대가 바뀌었거나 DB 유효값이 꺼졌으면 즉시 중단한다.
1. 평가 대상 제외(상태 NULL, 로그 없음): 계정 auto_reply_enabled=0·비활성, INBOX가 아닌 폴더(정크
   포함), `id ≤ watermark`, `is_backfill`(계정 최초 동기화), Date가 저장 시각보다 72시간 넘게 이전,
   이미 평가된 메일, LoopGuard 원자료 없음(이전 버전에서 받은 메일 — fail-closed).
2. LoopGuard 정적 조건 1~11(끌 수 없음) → blocked + 로그
3. 규칙을 priority 오름차순으로 평가, 처음 맞는 규칙 적용(규칙 계정 범위 포함)
4. 맞는 규칙이 없으면 `autoreply.unmatched_action`(기본 ignore, 설정으로 draft)
5. ignore → ignored(로그 없음)
6. **Phase B 단순화**: auto_send 예비판정(AuthGate·LoopGuard 동적 조건)은 하지 않는다. 계획은
   언제나 draft이며, 규칙 action이 auto_send면 강등 사유 `autosend_not_yet_supported`를 남긴다.
7. G3 잡 생성(writer 트랜잭션, `autoreply_repo.create_autoreply_job`).

의존 규칙(§4.2): rules는 mail을 import하지 않는다 — 주소 정규화 함수와(드라이런 전용) 헤더
원자료 폴백 함수는 app.py가 주입한다.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from emailtomcp.config.settings import get_setting
from emailtomcp.rules.loop_guard import LoopGuardInput, check_static
from emailtomcp.rules.regex_safe import BODY_SCAN_CHARS, regex_search
from emailtomcp.rules.schema import Condition, ConditionSet, RuleSchemaError, parse_conditions
from emailtomcp.storage.repositories import accounts as accounts_repo
from emailtomcp.storage.repositories import autoreply as autoreply_repo
from emailtomcp.storage.repositories import messages as messages_repo
from emailtomcp.storage.repositories import rules as rules_repo

if TYPE_CHECKING:
    import sqlite3

    from emailtomcp.core.clock import Clock
    from emailtomcp.core.events import EventBus
    from emailtomcp.storage.db import Database
    from emailtomcp.storage.repositories.messages import AutoReplyFacts
    from emailtomcp.storage.repositories.rules import RuleRow

logger = logging.getLogger(__name__)

EVENT_MESSAGE_RECEIVED = "MessageReceived"  # mail.sync_service와 같은 이름(이벤트 문자열)
SETTING_UNMATCHED_ACTION = "autoreply.unmatched_action"
MAX_DATE_SKEW = timedelta(hours=72)
REASON_AUTOSEND_NOT_YET = "autosend_not_yet_supported"

AddrNormalizer = Callable[[str], str]
HeadersFallback = Callable[[str], "dict[str, Any] | None"]

PlanKind = Literal["exclude", "blocked", "ignore", "draft"]


# ---------------------------------------------------------------- 순수 판정


@dataclass(frozen=True, slots=True)
class CompiledRule:
    row: RuleRow
    conditions: ConditionSet


@dataclass(frozen=True, slots=True)
class Plan:
    kind: PlanKind
    reason: str | None = None
    rule: RuleRow | None = None
    loop_reasons: tuple[str, ...] = ()
    downgrade_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MessageView:
    """조건 평가용 정규화 값."""

    from_norm: str
    from_domain: str
    to_norms: tuple[str, ...]
    cc_norms: tuple[str, ...]
    subject: str
    body: str
    has_attachment: bool
    received_local: datetime | None


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def domain_of(addr_norm: str | None) -> str:
    if not addr_norm or "@" not in addr_norm:
        return ""
    return addr_norm.rpartition("@")[2]


def build_view(facts: AutoReplyFacts, normalize: AddrNormalizer) -> MessageView:
    from_norm = facts.from_addr_norm or (normalize(facts.from_addr) if facts.from_addr else "")
    received = _parse_dt(facts.received_at)
    return MessageView(
        from_norm=from_norm,
        from_domain=domain_of(from_norm),
        to_norms=tuple(normalize(a) for a in _json_list(facts.to_addrs_json)),
        cc_norms=tuple(normalize(a) for a in _json_list(facts.cc_addrs_json)),
        subject=facts.subject or "",
        body=(facts.body_text or "")[:BODY_SCAN_CHARS],
        has_attachment=facts.has_attachments,
        received_local=received.astimezone() if received is not None else None,
    )


def build_loop_input(
    facts: AutoReplyFacts, headers_data: dict[str, Any] | None, normalize: AddrNormalizer
) -> LoopGuardInput:
    """G1 원자료 + 메일 행 → LoopGuard 입력. 원자료가 없으면 parse_error(차단)."""
    sender_norm = normalize(facts.sender_addr) if facts.sender_addr else None
    from_norm = facts.from_addr_norm or (normalize(facts.from_addr) if facts.from_addr else None)
    if headers_data is None:
        return LoopGuardInput(
            headers={},
            content_type="",
            report_parts=False,
            from_addr_norm=from_norm,
            from_count=facts.from_count,
            sender_addr_norm=sender_norm,
            subject=facts.subject,
            parse_error=True,
        )
    headers = headers_data.get("headers") or {}
    return LoopGuardInput(
        headers={str(k).lower(): list(v) for k, v in headers.items() if isinstance(v, list)},
        content_type=str(headers_data.get("content_type") or ""),
        report_parts=bool(headers_data.get("report_parts")),
        from_addr_norm=from_norm,
        from_count=facts.from_count,
        sender_addr_norm=sender_norm,
        subject=facts.subject,
        parse_error=bool(headers_data.get("parse_error")),
    )


def _text_op(op: str, value: str, target: str, compiled: Any) -> bool:
    if op == "regex":
        return regex_search(compiled, target)
    t = target.casefold()
    v = value.casefold()
    if op == "equals":
        return t == v
    if op == "contains":
        return v in t
    if op == "not_contains":
        return v not in t
    if op == "starts_with":
        return t.startswith(v)
    if op == "ends_with":
        return t.endswith(v)
    return False


def _between(value: list[int], x: int) -> bool:
    start, end = value
    if start < end:
        return start <= x < end
    return x >= start or x < end  # 자정(주말)을 넘는 구간


def _match_condition(cond: Condition, view: MessageView) -> bool:
    f, op, value = cond.field, cond.op, cond.value
    if f == "auth":
        return False  # AuthGate는 Phase C — 이번 단계는 항상 불일치(fail-closed)
    if f == "has_attachment":
        return view.has_attachment == value
    if f in ("received_hour", "received_weekday"):
        if view.received_local is None:
            return False
        x = view.received_local.hour if f == "received_hour" else view.received_local.weekday()
        return _between(value, x) if op == "between" else x == value
    targets: tuple[str, ...]
    if f == "from_address":
        targets = (view.from_norm,)
    elif f == "from_domain":
        targets = (view.from_domain,)
    elif f == "to":
        targets = view.to_norms
    elif f == "cc":
        targets = view.cc_norms
    elif f == "subject":
        targets = (view.subject,)
    elif f == "body":
        targets = (view.body,)
    else:
        return False
    if op == "in_list":
        items = set(value)
        return any(t.casefold() in items for t in targets)
    if op == "not_contains":
        return all(_text_op(op, value, t, cond.compiled) for t in targets)
    return any(_text_op(op, value, t, cond.compiled) for t in targets)


def match_rule(conditions: ConditionSet, view: MessageView) -> bool:
    if not conditions.conditions:
        return True
    results = (_match_condition(c, view) for c in conditions.conditions)
    return all(results) if conditions.match == "all" else any(results)


def compile_rules(rows: Iterable[RuleRow]) -> list[CompiledRule]:
    """저장된 규칙을 컴파일한다. 깨진 규칙은 건너뛴다(그 규칙은 맞지 않음 — fail-closed)."""
    compiled: list[CompiledRule] = []
    for row in rows:
        try:
            compiled.append(CompiledRule(row=row, conditions=parse_conditions(row.conditions_json)))
        except RuleSchemaError:
            logger.warning("규칙 #%s 조건을 읽을 수 없어 건너뜁니다", row.id)
    return compiled


def exclusion_reason(
    facts: AutoReplyFacts,
    *,
    headers_data: dict[str, Any] | None,
    account_auto_reply: bool,
    watermark: int,
) -> str | None:
    """§7.2 1단계. 평가 대상이 아니면 사유(로그에는 남기지 않음)."""
    if facts.auto_reply_status is not None:
        return "already_evaluated"
    if not account_auto_reply:
        return "account_auto_reply_off"
    if facts.folder_role != "inbox":
        return "not_inbox"
    if facts.id <= watermark:
        return "before_watermark"
    if headers_data is None:
        return "loop_headers_missing"
    if headers_data.get("is_backfill") is not False:
        return "backfill"
    date = _parse_dt(facts.date_hdr)
    received = _parse_dt(facts.received_at)
    if date is not None and received is not None and received - date > MAX_DATE_SKEW:
        return "too_old"
    return None


def plan_message(
    facts: AutoReplyFacts,
    *,
    loop_input: LoopGuardInput,
    rules: list[CompiledRule],
    my_addresses: frozenset[str],
    unmatched_action: str,
    normalize: AddrNormalizer,
) -> Plan:
    """§7.2 2~6단계(순수). Phase B: 계획은 blocked/ignore/draft뿐이다."""
    loop_reasons = check_static(loop_input, my_addresses)
    if loop_reasons:
        return Plan(kind="blocked", reason=loop_reasons[0], loop_reasons=tuple(loop_reasons))
    view = build_view(facts, normalize)
    for rule in rules:
        if rule.row.account_id is not None and rule.row.account_id != facts.account_id:
            continue
        if not match_rule(rule.conditions, view):
            continue
        if rule.row.action == "ignore":
            return Plan(kind="ignore", rule=rule.row)
        downgrade = REASON_AUTOSEND_NOT_YET if rule.row.action == "auto_send" else None
        return Plan(kind="draft", rule=rule.row, downgrade_reason=downgrade)
    if unmatched_action == "draft":
        return Plan(kind="draft", reason="unmatched")
    return Plan(kind="ignore", reason="unmatched")


def load_headers(facts: AutoReplyFacts) -> dict[str, Any] | None:
    """저장된 G1 원자료(JSON)를 읽는다. 없거나 형식이 다르면 None."""
    raw = facts.auto_headers_json
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("v") != 1:
        return None
    if not isinstance(value.get("headers"), dict):
        return None
    return value


def my_addresses(conn: sqlite3.Connection, normalize: AddrNormalizer) -> frozenset[str]:
    """모든 내 계정 주소와 별칭(LoopGuard 10번, 계정 간 루프 포함)."""
    result: set[str] = set()
    for account in accounts_repo.list_accounts(conn):
        result.add(normalize(account.email_address))
        result.update(normalize(a) for a in account.aliases if a)
    return frozenset(a for a in result if a)


def read_unmatched_action(conn: sqlite3.Connection) -> str:
    try:
        value = get_setting(conn, SETTING_UNMATCHED_ACTION, "ignore")
    except (TypeError, ValueError):
        return "ignore"
    return value if value in ("ignore", "draft") else "ignore"


# ---------------------------------------------------------------- 드라이런(§11.4)


@dataclass(frozen=True, slots=True)
class DryRunRow:
    message_id: int
    received_at: str
    from_addr: str | None
    subject: str | None
    matched: bool
    loop_reasons: tuple[str, ...]
    result: str  # "draft" | "ignore" | "blocked" | "no_match"
    note: str | None = None


@dataclass(frozen=True, slots=True)
class DryRunReport:
    total: int
    matched: int
    blocked: int
    would_draft: int
    rows: tuple[DryRunRow, ...] = field(default_factory=tuple)


def dry_run_rule(
    conn: sqlite3.Connection,
    *,
    conditions: ConditionSet,
    action: str,
    account_id: int | None,
    normalize: AddrNormalizer,
    headers_fallback: HeadersFallback | None,
    limit: int = 50,
) -> DryRunReport:
    """규칙 하나를 최근 INBOX 메일에 적용해 본다. claude 실행·DB 쓰기 없음(토글 off에서도 가능)."""
    facts_list = messages_repo.list_recent_inbox_facts(conn, account_id=account_id, limit=limit)
    mine = my_addresses(conn, normalize)
    rows: list[DryRunRow] = []
    matched = blocked = would_draft = 0
    for facts in facts_list:
        headers_data = load_headers(facts)
        note = None
        if headers_data is None and headers_fallback is not None:
            headers_data = headers_fallback(facts.eml_path)
            note = "이전 버전에서 받은 메일 — 원문에서 헤더를 다시 읽음"
        loop = check_static(build_loop_input(facts, headers_data, normalize), mine)
        is_match = match_rule(conditions, build_view(facts, normalize))
        if is_match:
            matched += 1
        if loop:
            blocked += 1
            result = "blocked"
        elif not is_match:
            result = "no_match"
        elif action == "ignore":
            result = "ignore"
        else:
            result = "draft"  # auto_send 규칙도 이번 버전에서는 초안
            would_draft += 1
        rows.append(
            DryRunRow(
                message_id=facts.id,
                received_at=facts.received_at,
                from_addr=facts.from_addr,
                subject=facts.subject,
                matched=is_match,
                loop_reasons=tuple(loop),
                result=result,
                note=note,
            )
        )
    return DryRunReport(
        total=len(rows),
        matched=matched,
        blocked=blocked,
        would_draft=would_draft,
        rows=tuple(rows),
    )


# ---------------------------------------------------------------- 런타임 엔진


class RuleEngine:
    """MessageReceived 구독자. AutoReplyController가 켤 때 `start`, 끌 때 `stop`한다."""

    def __init__(
        self,
        db: Database,
        *,
        clock: Clock,
        event_bus: EventBus,
        normalize_addr: AddrNormalizer,
        on_job_created: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._event_bus = event_bus
        self._normalize = normalize_addr
        self._on_job_created = on_job_created
        self._lock = threading.Lock()
        self._generation: int | None = None
        self._subscribed = False
        self.evaluations = 0  # 평가 호출 수(I2 검증·진단용)

    # ------------------------------------------------------------ 구독

    @property
    def subscribed(self) -> bool:
        return self._subscribed

    def set_job_listener(self, listener: Callable[[], None] | None) -> None:
        self._on_job_created = listener

    def start(self, generation: int) -> None:
        with self._lock:
            self._generation = generation
            if not self._subscribed:
                self._event_bus.subscribe(EVENT_MESSAGE_RECEIVED, self._on_message_received)
                self._subscribed = True

    def stop(self) -> None:
        with self._lock:
            self._generation = None
            if self._subscribed:
                self._event_bus.unsubscribe(EVENT_MESSAGE_RECEIVED, self._on_message_received)
                self._subscribed = False

    def _current_generation(self) -> int | None:
        with self._lock:
            return self._generation

    def _on_message_received(self, _name: str, payload: object) -> None:
        generation = self._current_generation()
        if generation is None or not isinstance(payload, dict):
            return
        message_id = payload.get("message_id")
        if not isinstance(message_id, int):
            return
        try:
            self.evaluate_message(message_id, expected_generation=generation)
        except Exception:  # noqa: BLE001 — 평가 실패가 수신 동기화를 막으면 안 된다
            logger.exception("자동회신 규칙 평가 실패: message_id=%s", message_id)

    # ------------------------------------------------------------ 평가

    def evaluate_message(self, message_id: int, *, expected_generation: int) -> str:
        """메일 한 통을 평가한다. 결과 문자열(진단·테스트용)을 돌려준다."""
        if self._current_generation() != expected_generation:
            return "stale"
        self.evaluations += 1
        conn = self._db.new_read_connection()
        try:
            toggle = autoreply_repo.get_toggle_state(conn)
            if not toggle.enabled or toggle.generation != expected_generation:
                return "stale"
            facts = messages_repo.get_autoreply_facts(conn, message_id)
            if facts is None:
                return "missing"
            account = accounts_repo.get_account(conn, facts.account_id)
            headers_data = load_headers(facts)
            excluded = exclusion_reason(
                facts,
                headers_data=headers_data,
                account_auto_reply=bool(account and account.enabled and account.auto_reply_enabled),
                watermark=toggle.watermark_message_id,
            )
            if excluded is not None:
                return f"excluded:{excluded}"
            rules = compile_rules(r for r in rules_repo.list_rules(conn) if r.enabled)
            plan = plan_message(
                facts,
                loop_input=build_loop_input(facts, headers_data, self._normalize),
                rules=rules,
                my_addresses=my_addresses(conn, self._normalize),
                unmatched_action=read_unmatched_action(conn),
                normalize=self._normalize,
            )
        finally:
            conn.close()

        if self._current_generation() != expected_generation:
            return "stale"  # 평가 중에 꺼짐(R-a) — 아무것도 쓰지 않는다
        now = self._clock.now()
        rule = plan.rule
        if plan.kind in ("blocked", "ignore"):
            autoreply_repo.record_evaluation_outcome(
                self._db,
                message_id=message_id,
                status="blocked" if plan.kind == "blocked" else "ignored",
                reason_code=plan.reason,
                rule_id=rule.id if rule else None,
                rule_version=rule.version if rule else None,
                sender_addr_norm=facts.from_addr_norm,
                thread_key=facts.thread_key,
                validation={"loop_reasons": list(plan.loop_reasons)} if plan.loop_reasons else None,
                expected_generation=expected_generation,
                now=now,
            )
            return plan.kind
        job_id = autoreply_repo.create_autoreply_job(
            self._db,
            message_id=message_id,
            rule_id=rule.id if rule else None,
            rule_version=rule.version if rule else None,
            planned_action="draft",
            downgrade_reason=plan.downgrade_reason,
            sender_addr_norm=facts.from_addr_norm,
            sender_domain=domain_of(facts.from_addr_norm) or None,
            thread_key=facts.thread_key,
            expected_generation=expected_generation,
            now=now,
        )
        if job_id is None:
            return "no_job"
        listener = self._on_job_created
        if listener is not None:
            try:
                listener()
            except Exception:  # noqa: BLE001
                logger.warning("잡 생성 알림 실패", exc_info=True)
        return "queued"

    def catch_up(self, *, expected_generation: int) -> int:
        """켜기 3단계·기동 시 1회 따라잡기: watermark 이후 아직 평가하지 않은 INBOX 메일."""
        conn = self._db.new_read_connection()
        try:
            toggle = autoreply_repo.get_toggle_state(conn)
            if not toggle.enabled or toggle.generation != expected_generation:
                return 0
            candidates = messages_repo.list_autoreply_catchup(
                conn, after_id=toggle.watermark_message_id
            )
        finally:
            conn.close()
        count = 0
        for facts in candidates:
            if self._current_generation() != expected_generation:
                break
            try:
                self.evaluate_message(facts.id, expected_generation=expected_generation)
                count += 1
            except Exception:  # noqa: BLE001
                logger.exception("따라잡기 평가 실패: message_id=%s", facts.id)
        return count
