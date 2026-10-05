"""자동회신 전역 토글 레포지토리 (DESIGN.md §7.0, §7.9, §7.11) — P3 Phase A.

이 모듈은 **`autoreply.enabled`/`autoreply.generation`/`autoreply.watermark_message_id`/
`autoreply.enabled_changed_at`을 쓰는 유일한 곳**이다(불변식 I6, 아키텍처 테스트로 강제).
`autoreply.queue_state`/`autoreply.autosend_state` 키 이름도 여기서 정의한다 — `app.py`의
보호 키 목록은 이 상수를 가져다 쓴다.

Phase A 범위(토글 인프라)
- `disable_and_drain`: 끄기 1단계(a~e)를 writer 트랜잭션 **하나**로 한다. **CAS 없음, 항상
  성공**(보안리뷰 L-A — 끄기는 안전한 방향이므로 세대가 달라도 거부하지 않는다).
- `enable`: compare-and-set. 설정창을 열 때 읽은 세대와 현재 세대가 다르면 PolicyError.
- `recover_on_startup`: 앱 기동 시 복구 트랜잭션.
- `drain_autosend_outbox`: `approved_by='policy_auto_send' AND status='outbox'` 초안을 draft로
  되돌린다. Phase A 시점에는 이런 행을 만드는 코드(G7 `commit_autoreply_result`)가 아직 없어
  실제 데이터에서는 항상 0건이지만, **조회·전이 본체는 지금 완성해 둔다**(빈 결과를 돌려주는
  자리표시자로 두면 Phase B/C가 G7을 먼저 붙였을 때 끄기가 outbox를 놓치는 fail-open 구간이
  생기기 때문이다). Phase B/C에서 추가할 것: `send_unknown`→draft 초기화 전 보강 로그
  (§7.3 `autosend_reset_unknown`)와, 설정 저장 경로(계정·규칙)에서의 호출(§7.9 M-A).

P3 Phase B(이번 단계)에서 추가한 것 — 아래 "Phase B" 절 참고
- `check_autoreply_gate`(§7.0·§7.11 단일 gate 함수). `rules/gate.py`는 이 함수를 그대로 다시
  내보낸다 — storage가 rules를 import할 수 없어(§4.2) G3·G4(storage)와 G7(rules 판정)이 **같은
  함수 객체**를 쓰려면 본체가 여기 있어야 한다.
- `create_autoreply_job`(G3), `record_evaluation_outcome`(G2 결과 ignored·blocked 기록),
  `claim_next_job`(G4), `mark_submitted`(G5), `AutoReplyRepository.commit_autoreply_result`(G7),
  러너용 종료 헬퍼(`cancel_running_job`·`fail_running_job`), 큐 상태(`autoreply.queue_state`)
  일시정지/재개, UI용 잡 목록.
- **Phase B 안전경계**: 이 모듈의 어떤 함수도 `approved_by`에 자동발송 값을 쓰지 않는다. G7은
  verdict가 `draft`일 때 `status='draft'` 초안만 INSERT하고, `AutoSendVerdict.decision`에는 자동발송
  값 자체가 없다(core/autoreply_types.py). G3도 planned_action이 draft가 아니면 거부한다.
  자동발송(G7 outbox 진입)은 SendService G8과 함께 Phase C에서 연다.

세대 규칙(§7.0 L-B)
- `autoreply.generation`은 m0003에서 1 이상으로 초기화되고 **늘어나기만 한다**. 끄기와 켜기 모두 +1.
- 유효한 세대는 bool이 아닌 **1 이상의 정수**뿐이다. 그 밖의 상태 — **키가 아예 없음**, 0,
  음수, 비정수, bool, JSON 손상 — 는 모두 "손상"으로 본다(§7.0 gate 규칙 "키가 없거나 정수가
  아니거나 1 미만이면 disabled"와 같은 기준, 데카르트 D1). 손상 상태에서는 켜기를 거부하며
  유효 상태(`ToggleState.enabled`)는 off로 본다(fail-closed). 끄기는 손상 상태에서도 성공한다
  (enabled=false만 쓰고 세대는 건드리지 않는다 — 손상·누락 값을 임의 숫자로 "복구"하면 세대가
  1부터 다시 시작되어 옛 잡의 enable_gen과 다시 일치할 수 있기 때문이다).
- m0003이 언제나 1 이상의 값을 넣어 두므로 정상 흐름에서는 키가 없거나 0인 상태가 생기지 않는다.
  그런 상태는 DB 파일 직접 조작으로만 생기며, 런타임에는 이를 복구(재설정)하는 경로를 두지 않는다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from emailtomcp.core.autoreply_types import (
    AutoReplyJobRow,
    AutoSendVerdict,
    GateResult,
    GuardResult,
    PreflightResult,
    ProposedReply,
)
from emailtomcp.core.errors import PolicyError
from emailtomcp.core.models import ApprovedBy
from emailtomcp.storage.transitions import transition

if TYPE_CHECKING:
    from emailtomcp.core.ports import AutoSendVerifier
    from emailtomcp.storage.db import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 설정 키(보호 키)

SETTING_ENABLED = "autoreply.enabled"
SETTING_GENERATION = "autoreply.generation"
SETTING_WATERMARK = "autoreply.watermark_message_id"
SETTING_ENABLED_CHANGED_AT = "autoreply.enabled_changed_at"
SETTING_QUEUE_STATE = "autoreply.queue_state"
SETTING_AUTOSEND_STATE = "autoreply.autosend_state"

# 범용 설정 쓰기(`UiApi.set_setting`)로 바꿀 수 없는 키(§7.0).
# app.py가 이 튜플을 보호 목록으로 쓴다.
PROTECTED_SETTING_KEYS: tuple[str, ...] = (
    SETTING_ENABLED,
    SETTING_GENERATION,
    SETTING_WATERMARK,
    SETTING_ENABLED_CHANGED_AT,
    SETTING_QUEUE_STATE,
    SETTING_AUTOSEND_STATE,
)

# 잡 취소·outbox 드레인 사유(auto_reply_log.reason_code, auto_reply_jobs.error).
REASON_DISABLED = "disabled"
REASON_DISABLED_AT_STARTUP = "disabled_at_startup"

_POLICY_AUTO_SEND = ApprovedBy.POLICY_AUTO_SEND.value
_ACTIVE_JOB_STATES = ("queued", "running")

# 큐 상태(§7.7). 이 모듈만 쓴다.
QUEUE_RUNNING = "running"
QUEUE_PAUSED_USER = "paused_user"
QUEUE_STATES: frozenset[str] = frozenset(
    {QUEUE_RUNNING, QUEUE_PAUSED_USER, "paused_auth", "stopped_emergency"}
)
AUTOSEND_PAUSED_SECURITY = "paused_security"

CAS_CONFLICT_MESSAGE = "다른 곳에서 바뀌었습니다. 다시 확인하세요"


# ---------------------------------------------------------------- 결과 타입


@dataclass(frozen=True, slots=True)
class ToggleState:
    """UI 표시용 읽기 전용 스냅샷. `enabled`는 **유효값**(저장값 ∧ 세대 ≥ 1)이다."""

    enabled: bool
    generation: int | None  # 손상(키 없음·1 미만·형식 오류)이면 None
    watermark_message_id: int
    enabled_changed_at: str | None


@dataclass(frozen=True, slots=True)
class DrainReport:
    """끄기 1단계 결과(§7.0 1e). 결과 정보창과 2단계(토큰 폐기·프로세스 종료)의 근거."""

    generation: int | None  # 끈 뒤의 세대(손상 상태였으면 None)
    cancelled_jobs: int  # queued·running에서 cancelled로 바꾼 kind=auto_reply 잡 수
    cancelled_running_job_ids: tuple[int, ...]  # 그중 running이었던 잡(2단계 토큰 폐기·kill 대상)
    reverted_outbox: int  # policy_auto_send outbox → draft 건수
    sending_in_flight: int  # 회수할 수 없는 origin=autoreply sending 건수


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """기동 복구 결과(§7.0 "앱 기동 시")."""

    enabled: bool  # 복구 트랜잭션 안에서 다시 읽은 유효값
    generation: int | None
    cancelled_jobs: int
    reverted_outbox: int
    requeued_jobs: int  # enabled=true일 때 running→queued(enable_gen은 원래 값 유지)


# ---------------------------------------------------------------- 내부 헬퍼(설정 읽기·쓰기)

# JSON 디코드에 실패한 설정값 표식(어떤 정상 값과도 같지 않다).
_CORRUPT = object()


def _read_raw(conn: sqlite3.Connection, key: str) -> tuple[bool, Any]:
    """(키 존재 여부, JSON 디코드 값). 디코드에 실패하면 (True, 형식오류 표식)."""
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return False, None
    try:
        return True, json.loads(row[0])
    except (TypeError, ValueError):
        return True, _CORRUPT


def _is_int(value: Any) -> bool:
    # bool은 int의 하위 타입이다 — true/false를 세대 1/0으로 읽지 않는다.
    return isinstance(value, int) and not isinstance(value, bool)


def _read_generation(conn: sqlite3.Connection) -> int | None:
    """현재 세대(1 이상의 정수). 키가 없거나 1 미만이거나 형식이 잘못됐으면 None(손상, D1).

    키 없음을 0으로 읽으면 "키 삭제 → 켜기/끄기"로 세대가 1부터 다시 시작되는 재설정 경로가
    생긴다(데카르트 D1 재현). m0003 이후 정상 DB에는 항상 1 이상의 값이 있으므로 손상으로 본다.
    """
    exists, value = _read_raw(conn, SETTING_GENERATION)
    if exists and _is_int(value) and value >= 1:
        return int(value)
    return None


def _read_stored_enabled(conn: sqlite3.Connection) -> bool:
    """저장된 enabled. **정확히 JSON true일 때만** True(없거나 형식 오류면 False, I7)."""
    exists, value = _read_raw(conn, SETTING_ENABLED)
    return exists and value is True


def _effective_enabled(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    """(유효 enabled, 세대). 저장값이 true여도 세대가 손상(없음·1 미만 포함)이면 off(§7.0 L-B ①)."""
    generation = _read_generation(conn)
    enabled = _read_stored_enabled(conn) and generation is not None
    return enabled, generation


def _read_watermark(conn: sqlite3.Connection) -> int:
    exists, value = _read_raw(conn, SETTING_WATERMARK)
    if exists and _is_int(value) and value >= 0:
        return int(value)
    return 0


def _write(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def _iso(now: datetime) -> str:
    return now.isoformat()


# ---------------------------------------------------------------- 공용 트랜잭션 조각


def _cancel_active_autoreply_jobs(
    conn: sqlite3.Connection, *, reason: str, now_iso: str, rule_id: int | None = None
) -> tuple[int, tuple[int, ...]]:
    """열린 트랜잭션 안에서 kind=auto_reply 잡 중 queued·running을 cancelled로 바꾼다(§7.0 1b·1d).

    잡마다 messages.auto_reply_status도 cancelled로 바꾸고(같은 트랜잭션, §5.2 "진실 소스는
    jobs이고 함께 갱신한다"), auto_reply_log에 outcome=cancelled를 남긴다. 커밋은 호출자 몫.

    Returns:
        (취소한 잡 수, 그중 running이었던 잡 ID 튜플)
    """
    placeholders = ",".join("?" for _ in _ACTIVE_JOB_STATES)
    sql = (
        "SELECT id, message_id, status, rule_id, rule_version, sender_addr_norm "
        f"FROM auto_reply_jobs WHERE kind = 'auto_reply' AND status IN ({placeholders})"
    )
    params: list[object] = list(_ACTIVE_JOB_STATES)
    if rule_id is not None:
        sql += " AND rule_id = ?"
        params.append(rule_id)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    cancelled = 0
    running_ids: list[int] = []
    for row in rows:
        job_id = int(row["id"])
        if not transition(
            conn, "auto_reply_jobs", job_id, _ACTIVE_JOB_STATES, "cancelled", commit=False
        ):
            continue  # 단일 writer 안이라 사실상 없지만, 전이 경합이면 건드리지 않는다
        cancelled += 1
        if row["status"] == "running":
            running_ids.append(job_id)
        conn.execute(
            "UPDATE auto_reply_jobs SET error = ?, error_class = 'policy', finished_at = ? "
            "WHERE id = ?",
            (reason, now_iso, job_id),
        )
        transition(
            conn, "messages", int(row["message_id"]), _ACTIVE_JOB_STATES, "cancelled", commit=False
        )
        conn.execute(
            "INSERT INTO auto_reply_log (ts, message_id, job_id, rule_id, rule_version, "
            "sender_addr_norm, outcome, reason_code) VALUES (?, ?, ?, ?, ?, ?, 'cancelled', ?)",
            (
                now_iso,
                row["message_id"],
                job_id,
                row["rule_id"],
                row["rule_version"],
                row["sender_addr_norm"],
                reason,
            ),
        )
    return cancelled, tuple(running_ids)


def drain_autosend_outbox(
    conn: sqlite3.Connection, *, account_id: int | None, rule_id: int | None, reason: str
) -> int:
    """열린 writer 트랜잭션 안에서 `approved_by='policy_auto_send' AND status='outbox'` 초안을
    draft로 되돌린다(§7.0 1c, §7.7 긴급정지, §7.9 M-A 설정 변경 드레인). 커밋은 호출자 몫.

    - `account_id`/`rule_id`가 None이면 그 조건으로 거르지 않는다(끄기·긴급정지는 둘 다 None).
      `rule_id`는 초안의 `job_id`가 가리키는 잡의 rule_id로 거른다. 단, **어느 규칙에서 왔는지
      확인할 수 없는 행**(job_id가 NULL, 잡이 삭제됨, 잡의 rule_id가 NULL)도 함께 되돌린다 —
      규칙 범위 드레인에서 이런 고아 행을 빠뜨리면 fail-open이 되기 때문이다(보안검토 L-6).
      draft로 되돌리는 방향이라 더 넓게 잡아도 안전하다.
    - 전이는 `storage.transitions.transition()`으로 하므로 approved_by·approved_at·
      outbox_expires_at이 같은 UPDATE에서 NULL이 된다(I8, §5.3 초기화 규칙).
    - sending은 회수할 수 없으므로 대상이 아니다(§7.0 R-f). send_unknown·failed도 이 함수의
      대상이 아니다(사용자 확인 경로, §5.3).

    Phase A 주의: 이런 초안을 만드는 G7(`commit_autoreply_result`)은 Phase B/C에서 구현한다.
    지금은 실제 데이터에서 항상 0을 돌려주지만, 본체는 완성해 두었다(모듈 docstring 참고).
    """
    sql = (
        "SELECT d.id FROM drafts d LEFT JOIN auto_reply_jobs j ON j.id = d.job_id "
        "WHERE d.status = 'outbox' AND d.approved_by = ?"
    )
    params: list[object] = [_POLICY_AUTO_SEND]
    if account_id is not None:
        sql += " AND d.account_id = ?"
        params.append(account_id)
    if rule_id is not None:
        # 고아 행(job_id NULL·잡 삭제·잡 rule_id NULL)도 포함한다(L-6, fail-closed).
        sql += " AND (j.rule_id = ? OR j.id IS NULL OR j.rule_id IS NULL)"
        params.append(rule_id)
    sql += " ORDER BY d.id"
    draft_ids = [int(row[0]) for row in conn.execute(sql, params).fetchall()]
    reverted = 0
    for draft_id in draft_ids:
        if transition(conn, "drafts", draft_id, ["outbox"], "draft", commit=False):
            reverted += 1
            conn.execute(
                "UPDATE drafts SET last_error = ? WHERE id = ?",
                (f"자동발송 취소({reason})", draft_id),
            )
    return reverted


def _count_autoreply_sending(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM drafts WHERE status = 'sending' AND origin = 'autoreply'"
    ).fetchone()
    return int(row[0])


def _run_write_txn(db: Database, fn):  # noqa: ANN001, ANN202 — 내부 전용
    """writer 스레드에서 `BEGIN` → fn(conn) → `COMMIT`. 예외면 전부 롤백한다."""

    def _work(conn: sqlite3.Connection):  # noqa: ANN202
        conn.execute("BEGIN")
        try:
            result = fn(conn)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return result

    return db.writer.submit(_work).result()


# ---------------------------------------------------------------- 공개 API


def get_toggle_state(conn: sqlite3.Connection) -> ToggleState:
    """읽기 전용 조회(UI 표시용). `enabled`는 유효값이다(§7.0 L-B fail-closed)."""
    enabled, generation = _effective_enabled(conn)
    exists, changed_at = _read_raw(conn, SETTING_ENABLED_CHANGED_AT)
    return ToggleState(
        enabled=enabled,
        generation=generation,
        watermark_message_id=_read_watermark(conn),
        enabled_changed_at=changed_at if exists and isinstance(changed_at, str) else None,
    )


def disable_and_drain(db: Database, *, now: datetime) -> DrainReport:
    """끄기 1단계(§7.0 1a~1e)를 writer 트랜잭션 하나로 실행한다. **CAS 없음, 항상 성공**(L-A).

    a. enabled=false, generation += 1(손상·누락 상태면 세대는 그대로 — 새로 쓰지 않는다),
       enabled_changed_at=now
    b. kind=auto_reply 잡 queued·running → cancelled(error='disabled'), 메일 상태도 cancelled
    c. policy_auto_send outbox → draft(초기화 규칙은 transition()이 적용)
    d. 취소한 잡마다 auto_reply_log(outcome=cancelled, reason_code='disabled')
    e. 커밋하고 DrainReport를 돌려준다

    이미 꺼져 있어도 같은 절차를 다시 한다(세대도 올린다) — 끄기를 반복해도 안전하고, 남아 있던
    잡·outbox가 있으면 정리된다. 세대는 늘어나기만 하므로 단조성은 유지된다.
    """
    now_iso = _iso(now)

    def _txn(conn: sqlite3.Connection) -> DrainReport:
        generation = _read_generation(conn)
        _write(conn, SETTING_ENABLED, False)
        new_generation: int | None = None
        if generation is not None:
            new_generation = generation + 1
            _write(conn, SETTING_GENERATION, new_generation)
        _write(conn, SETTING_ENABLED_CHANGED_AT, now_iso)
        cancelled, running_ids = _cancel_active_autoreply_jobs(
            conn, reason=REASON_DISABLED, now_iso=now_iso
        )
        reverted = drain_autosend_outbox(
            conn, account_id=None, rule_id=None, reason=REASON_DISABLED
        )
        return DrainReport(
            generation=new_generation,
            cancelled_jobs=cancelled,
            cancelled_running_job_ids=running_ids,
            reverted_outbox=reverted,
            sending_in_flight=_count_autoreply_sending(conn),
        )

    return _run_write_txn(db, _txn)


def enable(db: Database, *, expected_generation: int, now: datetime) -> int:
    """켜기 2단계(§7.0): compare-and-set으로 켜고 **새 세대 번호**를 돌려준다.

    - 현재 세대가 `expected_generation`과 다르면 PolicyError(아무것도 바꾸지 않음, R-h).
    - 세대 값이 손상됐으면 PolicyError(fail-closed). **키가 아예 없거나 0인 경우도 손상**이다(D1) —
      m0003이 1 이상을 넣어 두므로 정상 DB의 최초 켜기(세대 1 → 2)는 영향을 받지 않는다.
    - 이미 켜져 있으면 PolicyError — 같은 세대로 두 번 켜면 watermark가 앞으로 밀려 아직
      평가하지 않은 메일을 건너뛸 수 있으므로 거부한다.
    - 성공하면 enabled=true, generation += 1(m0003이 1로 초기화, 이후 첫 켜기는 1→2),
      watermark=MAX(messages.id)(없으면 0), enabled_changed_at=now.
      queue_state·autosend_state는 건드리지 않는다.
    """
    if not _is_int(expected_generation):
        raise PolicyError("expected_generation은 정수여야 합니다")
    now_iso = _iso(now)

    def _txn(conn: sqlite3.Connection) -> int:
        generation = _read_generation(conn)
        if generation is None:
            raise PolicyError("자동회신 세대 정보가 손상되어 켤 수 없습니다")
        if generation != expected_generation:
            raise PolicyError(CAS_CONFLICT_MESSAGE)
        # 유효값 기준으로 판단한다(저장값 true ∧ 유효 세대). 세대가 손상이면 위에서 이미 거부했다.
        if _effective_enabled(conn)[0]:
            raise PolicyError("자동회신이 이미 켜져 있습니다. " + CAS_CONFLICT_MESSAGE)
        new_generation = generation + 1
        watermark = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])
        _write(conn, SETTING_ENABLED, True)
        _write(conn, SETTING_GENERATION, new_generation)
        _write(conn, SETTING_WATERMARK, watermark)
        _write(conn, SETTING_ENABLED_CHANGED_AT, now_iso)
        # queue_state가 한 번도 정해진 적 없으면(m0003이 v2 잔존값을 지움, L-5) 여기서 한 번만
        # running으로 초기화한다. 이미 값이 있으면(paused_user 등) 건드리지 않는다(§7.0 켜기 2단계).
        # autosend_state는 Phase B에서 만들지 않는다 — 없으면 자동발송 불가로 본다(fail-closed).
        exists, _value = _read_raw(conn, SETTING_QUEUE_STATE)
        if not exists:
            _write(conn, SETTING_QUEUE_STATE, QUEUE_RUNNING)
        return new_generation

    return _run_write_txn(db, _txn)


def recover_on_startup(db: Database, *, enabled: bool) -> RecoveryReport:
    """앱 기동 시 복구 트랜잭션(§7.0 "앱 기동 시").

    `enabled`는 호출자(컨트롤러)가 기동 시 읽은 값이지만, 트랜잭션 안에서 **유효값을 다시 읽어**
    둘 다 true일 때만 켜진 상태로 본다(fail-closed — 호출자 인자만 믿고 잡을 살리지 않는다).

    - off: kind=auto_reply queued·running → cancelled(reason=`disabled_at_startup`),
      policy_auto_send outbox → draft(초기화 규칙). **queued로 되돌리지 않는다.**
    - on: kind=auto_reply running → queued(재시작 복구, §2(c)). `enable_gen`은 원래 값을 유지한다
      (현재 세대로 덮어쓰지 않는다 — 세대가 다르면 Phase B의 G4에서 cancelled된다).
    """

    def _txn(conn: sqlite3.Connection) -> RecoveryReport:
        stored_enabled, generation = _effective_enabled(conn)
        effective = bool(enabled) and stored_enabled
        if not effective:
            now_iso = _iso(datetime.now(UTC))
            cancelled, _running = _cancel_active_autoreply_jobs(
                conn, reason=REASON_DISABLED_AT_STARTUP, now_iso=now_iso
            )
            reverted = drain_autosend_outbox(
                conn, account_id=None, rule_id=None, reason=REASON_DISABLED_AT_STARTUP
            )
            return RecoveryReport(
                enabled=False,
                generation=generation,
                cancelled_jobs=cancelled,
                reverted_outbox=reverted,
                requeued_jobs=0,
            )
        running = conn.execute(
            "SELECT id, message_id FROM auto_reply_jobs "
            "WHERE kind = 'auto_reply' AND status = 'running' ORDER BY id"
        ).fetchall()
        requeued = 0
        for row in running:
            if transition(
                conn, "auto_reply_jobs", int(row["id"]), ["running"], "queued", commit=False
            ):
                requeued += 1
                transition(
                    conn, "messages", int(row["message_id"]), ["running"], "queued", commit=False
                )
        return RecoveryReport(
            enabled=True,
            generation=generation,
            cancelled_jobs=0,
            reverted_outbox=0,
            requeued_jobs=requeued,
        )

    return _run_write_txn(db, _txn)


def preflight_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """켜기 확인창의 사전점검 숫자(§7.0 켜기 1단계). 켜기를 막지 않는 참고 정보다.

    지금 DB에 있는 테이블·컬럼으로 셀 수 있는 것만 센다. 규칙 편집 UI가 아직 없어(Phase B)
    활성 규칙 수는 지금은 사실상 0이다.
    """

    def _count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "autoreply_accounts": _count(
            "SELECT COUNT(*) FROM accounts WHERE auto_reply_enabled = 1 AND enabled = 1"
        ),
        "rules_total": _count("SELECT COUNT(*) FROM rules"),
        "rules_enabled": _count("SELECT COUNT(*) FROM rules WHERE enabled = 1"),
        "rules_auto_send": _count(
            "SELECT COUNT(*) FROM rules WHERE enabled = 1 AND action = 'auto_send'"
        ),
        # trusted_authserv_id·trusted_boundary_by 중 하나라도 없으면 auto_send가
        # draft로 강등된다(A1).
        "accounts_without_trust": _count(
            "SELECT COUNT(*) FROM accounts WHERE auto_reply_enabled = 1 AND enabled = 1 "
            "AND (trusted_authserv_id IS NULL OR trusted_authserv_id = '' "
            "OR trusted_boundary_by IS NULL OR trusted_boundary_by = '')"
        ),
    }


# ================================================================ Phase B
#
# 규칙엔진·잡 파이프라인(§7.2, §7.9 G2~G7). 모든 쓰기 함수는 writer 트랜잭션 하나로 실행하고,
# 판정은 그 트랜잭션이 방금 읽은 행으로 한다(P2 H-2). Phase B 안전경계는 모듈 docstring 참고.

REASON_RULE_DELETED = "rule_deleted"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# 잡 생성 상한(§7.7). jobs.created_at 기준, kind·status 무관 전부.
JOB_CAP_PER_SENDER_HOURLY = 3
JOB_CAP_PER_DOMAIN_HOURLY = 10
JOB_CAP_TOTAL_HOURLY = 30
JOB_CAP_QUEUE_LENGTH = 50


def sha256_text(text: str) -> str:
    """제출 본문 해시(G5·G7 공용). UTF-8 바이트 기준."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- gate(§7.0 유효 조건)


def check_autoreply_gate(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    rule_id: int | None,
    rule_version: int | None,
    job_generation: int | None,
) -> GateResult:
    """§7.0 마스터 스위치 판정. **순수 함수**(받은 conn만 읽고 쓰지 않는다).

    G3(잡 생성)·G4(실행 시작)·G7(결과 확정, `rules/autosend_verify.py` 경유)이 이 함수 하나를
    공유한다(`rules/gate.py`는 이 함수 객체를 그대로 다시 내보낸다). 판정 순서와 사유:

    1. 전역 토글 유효값(저장값 true ∧ 세대 ≥ 1)이 아니면 `disabled`(L-B ①, fail-closed)
    2. job_generation이 None·0·bool·비정수이거나 현재 세대와 다르면 `generation_mismatch`(L-B ②)
    3. 계정이 없거나 비활성이거나 auto_reply_enabled=0이면 `account_disabled`
    4. rule_id가 있으면: 규칙이 없으면 `rule_missing`, 비활성이면 `rule_disabled`, version이
       다르거나(None 포함) 규칙의 계정 범위가 이 계정을 벗어나면 `rule_changed`.
       rule_id가 None인데 rule_version만 있으면 모순이라 `rule_missing`.
    """
    enabled, generation = _effective_enabled(conn)
    if not enabled or generation is None:
        return GateResult(ok=False, reason_code="disabled")
    if (
        job_generation is None
        or not _is_int(job_generation)
        or job_generation < 1
        or job_generation != generation
    ):
        return GateResult(ok=False, reason_code="generation_mismatch")
    account = conn.execute(
        "SELECT auto_reply_enabled, enabled FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None or not account[0] or not account[1]:
        return GateResult(ok=False, reason_code="account_disabled")
    if rule_id is None:
        if rule_version is not None:
            return GateResult(ok=False, reason_code="rule_missing")
        return GateResult(ok=True)
    rule = conn.execute(
        "SELECT enabled, version, account_id FROM rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if rule is None:
        return GateResult(ok=False, reason_code="rule_missing")
    if not rule[0]:
        return GateResult(ok=False, reason_code="rule_disabled")
    if not _is_int(rule_version) or int(rule[1]) != rule_version:
        return GateResult(ok=False, reason_code="rule_changed")
    if rule[2] is not None and int(rule[2]) != account_id:
        return GateResult(ok=False, reason_code="rule_changed")
    return GateResult(ok=True)


# ---------------------------------------------------------------- 큐 상태(§7.7)


def get_queue_state(conn: sqlite3.Connection) -> str | None:
    """`autoreply.queue_state`. 없거나 형식이 틀리면 None(실행하지 않음, fail-closed)."""
    exists, value = _read_raw(conn, SETTING_QUEUE_STATE)
    if exists and isinstance(value, str) and value in QUEUE_STATES:
        return value
    return None


def get_autosend_state(conn: sqlite3.Connection) -> str | None:
    """`autoreply.autosend_state`(표시용). Phase B에는 자동발송이 없어 판정에 쓰지 않는다."""
    exists, value = _read_raw(conn, SETTING_AUTOSEND_STATE)
    return value if exists and isinstance(value, str) else None


def set_queue_paused(db: Database, *, paused: bool) -> str:
    """사용자 일시정지/재개(§11.5). running ↔ paused_user만 바꾼다.

    stopped_emergency·paused_auth는 이 경로로 풀 수 없다(PolicyError). 값이 없으면(한 번도
    초기화되지 않음) 일시정지는 paused_user, 재개는 running으로 쓴다.
    """

    def _txn(conn: sqlite3.Connection) -> str:
        current = get_queue_state(conn)
        if current not in (None, QUEUE_RUNNING, QUEUE_PAUSED_USER):
            raise PolicyError(f"지금 큐 상태({current})는 여기서 바꿀 수 없습니다")
        target = QUEUE_PAUSED_USER if paused else QUEUE_RUNNING
        _write(conn, SETTING_QUEUE_STATE, target)
        return target

    return _run_write_txn(db, _txn)


def _pause_autosend_security(conn: sqlite3.Connection) -> None:
    """허용 외 도구 등 보안 실패 시 autosend_state=paused_security(§2(c) C-10)."""
    _write(conn, SETTING_AUTOSEND_STATE, AUTOSEND_PAUSED_SECURITY)


def pause_autosend_for_security(db: Database) -> None:
    """잡 디렉터리 삭제 실패 등 트랜잭션 밖에서 생긴 보안 사유(§7.11 fail-closed)."""
    _run_write_txn(db, _pause_autosend_security)


# ---------------------------------------------------------------- 로그(auto_reply_log)


def _insert_log(
    conn: sqlite3.Connection,
    *,
    ts: str,
    outcome: str,
    message_id: int | None,
    job_id: int | None = None,
    rule_id: int | None = None,
    rule_version: int | None = None,
    sender_addr_norm: str | None = None,
    reason_code: str | None = None,
    recipient_basis: str | None = None,
    validation: dict[str, Any] | None = None,
    claude_turns: int | None = None,
    tools_used: tuple[str, ...] | None = None,
    body_len: int | None = None,
    body_sha256: str | None = None,
    recipient_norm: str | None = None,
    thread_key: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO auto_reply_log (ts, message_id, job_id, rule_id, rule_version, "
        "sender_addr_norm, outcome, reason_code, recipient_basis, validation_json, "
        "claude_turns, tools_used_json, body_len, body_sha256, recipient_norm, thread_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            message_id,
            job_id,
            rule_id,
            rule_version,
            sender_addr_norm,
            outcome,
            reason_code,
            recipient_basis,
            json.dumps(validation, ensure_ascii=False) if validation is not None else None,
            claude_turns,
            json.dumps(list(tools_used), ensure_ascii=False) if tools_used is not None else None,
            body_len,
            body_sha256,
            recipient_norm,
            thread_key,
        ),
    )


# ---------------------------------------------------------------- G2 결과 기록 / G3 잡 생성


def _set_initial_message_status(conn: sqlite3.Connection, message_id: int, status: str) -> bool:
    """평가 전(NULL)인 메일에만 첫 상태를 쓴다(NULL은 IN 목록으로 못 잡아 transition() 대신)."""
    cursor = conn.execute(
        "UPDATE messages SET auto_reply_status = ? WHERE id = ? AND auto_reply_status IS NULL",
        (status, message_id),
    )
    return cursor.rowcount == 1


def _toggle_matches(conn: sqlite3.Connection, expected_generation: int) -> bool:
    enabled, generation = _effective_enabled(conn)
    return enabled and generation is not None and generation == expected_generation


def record_evaluation_outcome(
    db: Database,
    *,
    message_id: int,
    status: Literal["ignored", "blocked"],
    reason_code: str | None,
    rule_id: int | None,
    rule_version: int | None,
    sender_addr_norm: str | None,
    thread_key: str | None,
    validation: dict[str, Any] | None,
    expected_generation: int,
    now: datetime,
) -> bool:
    """G2 결과(잡 없이 끝난 평가)를 기록한다. blocked만 로그를 남긴다(§5.2, ignored는 양이 많음).

    평가 도중 토글이 꺼졌거나 세대가 바뀌었으면 아무것도 쓰지 않는다(R-a와 같은 방향).
    """
    if status not in ("ignored", "blocked"):
        raise ValueError(f"평가 결과 상태가 아닙니다: {status}")

    def _txn(conn: sqlite3.Connection) -> bool:
        if not _toggle_matches(conn, expected_generation):
            return False
        if not _set_initial_message_status(conn, message_id, status):
            return False
        if status == "blocked":
            _insert_log(
                conn,
                ts=_iso(now),
                outcome="blocked",
                message_id=message_id,
                rule_id=rule_id,
                rule_version=rule_version,
                sender_addr_norm=sender_addr_norm,
                reason_code=reason_code,
                validation=validation,
                thread_key=thread_key,
            )
        return True

    return _run_write_txn(db, _txn)


def _job_cap_reason(
    conn: sqlite3.Connection,
    *,
    sender_addr_norm: str | None,
    sender_domain: str | None,
    now: datetime,
) -> str | None:
    """잡 생성 상한(§7.7). DB 집계(created_at 기준, status 무관). 넘으면 사유 코드."""
    since = _iso(now - timedelta(hours=1))

    def _count(sql: str, params: tuple[object, ...]) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    if (
        sender_addr_norm
        and _count(
            "SELECT COUNT(*) FROM auto_reply_jobs WHERE sender_addr_norm = ? AND created_at >= ?",
            (sender_addr_norm, since),
        )
        >= JOB_CAP_PER_SENDER_HOURLY
    ):
        return "job_cap_sender"
    if (
        sender_domain
        and _count(
            "SELECT COUNT(*) FROM auto_reply_jobs WHERE sender_domain = ? AND created_at >= ?",
            (sender_domain, since),
        )
        >= JOB_CAP_PER_DOMAIN_HOURLY
    ):
        return "job_cap_domain"
    total = _count("SELECT COUNT(*) FROM auto_reply_jobs WHERE created_at >= ?", (since,))
    if total >= JOB_CAP_TOTAL_HOURLY:
        return "job_cap_total"
    queued = _count("SELECT COUNT(*) FROM auto_reply_jobs WHERE status = 'queued'", ())
    if queued >= JOB_CAP_QUEUE_LENGTH:
        return "job_cap_queue"
    return None


def create_autoreply_job(
    db: Database,
    *,
    message_id: int,
    rule_id: int | None,
    rule_version: int | None,
    planned_action: str,
    downgrade_reason: str | None,
    sender_addr_norm: str | None,
    sender_domain: str | None,
    thread_key: str | None,
    expected_generation: int,
    now: datetime,
) -> int | None:
    """G3 잡 생성(§7.9). writer 트랜잭션 하나에서 gate·메일당 1잡·잡 생성 상한을 확인한다.

    Returns:
        새 잡 ID. 만들지 않았으면 None — gate 불통과(메일 상태 NULL 유지, R-a), 이미 평가됨,
        유니크 인덱스 충돌(`ux_jobs_autoreply_msg`, 따라잡기·이중 구독에도 멱등), 상한 초과
        (메일 blocked + 로그 `job_cap_*`).

    Phase B: planned_action은 `draft`만 받는다. 자동발송 계획은 G2(RuleEngine)가 이미 draft로
    강등해 사유(`autosend_not_yet_supported`)와 함께 넘긴다. 다른 값이면 PolicyError.
    """
    if planned_action != "draft":
        raise PolicyError("이번 버전에서는 초안(draft) 잡만 만들 수 있습니다")
    if not _is_int(expected_generation):
        raise PolicyError("expected_generation은 정수여야 합니다")
    now_iso = _iso(now)

    def _txn(conn: sqlite3.Connection) -> int | None:
        message = conn.execute(
            "SELECT account_id, auto_reply_status FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if message is None:
            return None
        gate = check_autoreply_gate(
            conn,
            account_id=int(message["account_id"]),
            rule_id=rule_id,
            rule_version=rule_version,
            job_generation=expected_generation,
        )
        if not gate.ok:
            return None
        if message["auto_reply_status"] is not None:
            return None
        if conn.execute(
            "SELECT 1 FROM auto_reply_jobs WHERE message_id = ? AND kind = 'auto_reply'",
            (message_id,),
        ).fetchone():
            return None
        cap = _job_cap_reason(
            conn, sender_addr_norm=sender_addr_norm, sender_domain=sender_domain, now=now
        )
        if cap is not None:
            _set_initial_message_status(conn, message_id, "blocked")
            _insert_log(
                conn,
                ts=now_iso,
                outcome="blocked",
                message_id=message_id,
                rule_id=rule_id,
                rule_version=rule_version,
                sender_addr_norm=sender_addr_norm,
                reason_code=cap,
                thread_key=thread_key,
            )
            return None
        try:
            cursor = conn.execute(
                "INSERT INTO auto_reply_jobs (kind, message_id, rule_id, rule_version, "
                "planned_action, downgrade_reason, sender_addr_norm, sender_domain, status, "
                "attempts, created_at, enable_gen) "
                "VALUES ('auto_reply', ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
                (
                    message_id,
                    rule_id,
                    rule_version,
                    planned_action,
                    downgrade_reason,
                    sender_addr_norm,
                    sender_domain,
                    now_iso,
                    expected_generation,
                ),
            )
        except sqlite3.IntegrityError:
            return None  # 메일당 1잡(유니크 인덱스) — 이미 다른 경로가 만들었다
        if not _set_initial_message_status(conn, message_id, "queued"):
            raise PolicyError("메일 상태 경합")  # 롤백(단일 writer 안이라 사실상 없음)
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    return _run_write_txn(db, _txn)


# ---------------------------------------------------------------- 잡 행 읽기


_JOB_SELECT = (
    "SELECT j.id, j.kind, j.message_id, m.account_id, j.rule_id, j.rule_version, "
    "j.planned_action, j.downgrade_reason, j.status, j.attempts, j.enable_gen, j.started_at, "
    "j.submitted_at, j.submission_sha256, j.sender_addr_norm "
    "FROM auto_reply_jobs j JOIN messages m ON m.id = j.message_id"
)


def _row_to_job(row: sqlite3.Row) -> AutoReplyJobRow:
    return AutoReplyJobRow(
        id=int(row["id"]),
        kind=row["kind"],
        message_id=int(row["message_id"]),
        account_id=int(row["account_id"]),
        rule_id=row["rule_id"],
        rule_version=row["rule_version"],
        planned_action=row["planned_action"],
        downgrade_reason=row["downgrade_reason"],
        status=row["status"],
        attempts=int(row["attempts"]),
        enable_gen=int(row["enable_gen"]),
        started_at=row["started_at"],
        submitted_at=row["submitted_at"],
        submission_sha256=row["submission_sha256"],
        sender_addr_norm=row["sender_addr_norm"],
    )


def get_job(conn: sqlite3.Connection, job_id: int) -> AutoReplyJobRow | None:
    row = conn.execute(f"{_JOB_SELECT} WHERE j.id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


# ---------------------------------------------------------------- 종료 헬퍼(트랜잭션 안)


def _end_job_cancelled(
    conn: sqlite3.Connection,
    job: AutoReplyJobRow,
    *,
    from_states: tuple[str, ...],
    reason: str,
    now_iso: str,
) -> bool:
    if not transition(conn, "auto_reply_jobs", job.id, from_states, "cancelled", commit=False):
        return False
    conn.execute(
        "UPDATE auto_reply_jobs SET error = ?, error_class = 'policy', finished_at = ? "
        "WHERE id = ?",
        (reason, now_iso, job.id),
    )
    transition(conn, "messages", job.message_id, from_states, "cancelled", commit=False)
    _insert_log(
        conn,
        ts=now_iso,
        outcome="cancelled",
        message_id=job.message_id,
        job_id=job.id,
        rule_id=job.rule_id,
        rule_version=job.rule_version,
        sender_addr_norm=job.sender_addr_norm,
        reason_code=reason,
    )
    return True


def _end_job_failed(
    conn: sqlite3.Connection,
    job: AutoReplyJobRow,
    *,
    error_class: str,
    reason: str,
    discard: bool,
    pause_security: bool,
    now_iso: str,
    validation: dict[str, Any] | None = None,
) -> bool:
    if not transition(conn, "auto_reply_jobs", job.id, ["running"], "failed", commit=False):
        return False
    conn.execute(
        "UPDATE auto_reply_jobs SET error = ?, error_class = ?, outcome = ?, finished_at = ? "
        "WHERE id = ?",
        (reason, error_class, "discarded" if discard else None, now_iso, job.id),
    )
    transition(conn, "messages", job.message_id, ["running"], "failed", commit=False)
    _insert_log(
        conn,
        ts=now_iso,
        outcome="failed",
        message_id=job.message_id,
        job_id=job.id,
        rule_id=job.rule_id,
        rule_version=job.rule_version,
        sender_addr_norm=job.sender_addr_norm,
        reason_code=reason,
        validation=validation,
    )
    if pause_security:
        _pause_autosend_security(conn)
    return True


def cancel_active_jobs_for_rule(
    conn: sqlite3.Connection, *, rule_id: int, reason: str, now_iso: str
) -> int:
    """규칙 삭제 트랜잭션 안에서 그 규칙의 queued·running 잡을 cancelled로(같은 conn, 미커밋)."""
    cancelled, _running = _cancel_active_autoreply_jobs(
        conn, reason=reason, now_iso=now_iso, rule_id=rule_id
    )
    return cancelled


# ---------------------------------------------------------------- G4 실행 시작


def claim_next_job(db: Database, *, now: datetime) -> AutoReplyJobRow | None:
    """G4(§7.9): queued → running. writer 트랜잭션 안에서 gate·`enable_gen == generation`·
    queue_state=running을 확인한다. gate 불통과 잡은 그 자리에서 cancelled로 끝낸다.

    queue_state가 running이 아니면(일시정지·미초기화 포함) 아무것도 가져가지 않는다 — 잡은 queued로
    남는다. 잡 토큰은 이 트랜잭션이 **커밋된 뒤에** 러너가 발급한다.
    """
    now_iso = _iso(now)

    def _txn(conn: sqlite3.Connection) -> AutoReplyJobRow | None:
        if get_queue_state(conn) != QUEUE_RUNNING:
            return None
        rows = conn.execute(
            f"{_JOB_SELECT} WHERE j.kind = 'auto_reply' AND j.status = 'queued' "
            "AND (j.next_run_at IS NULL OR j.next_run_at <= ?) ORDER BY j.id LIMIT 50",
            (now_iso,),
        ).fetchall()
        for row in rows:
            job = _row_to_job(row)
            gate = check_autoreply_gate(
                conn,
                account_id=job.account_id,
                rule_id=job.rule_id,
                rule_version=job.rule_version,
                job_generation=job.enable_gen,
            )
            if not gate.ok:
                _end_job_cancelled(
                    conn,
                    job,
                    from_states=("queued",),
                    reason=gate.reason_code or "gate",
                    now_iso=now_iso,
                )
                continue
            if not transition(conn, "auto_reply_jobs", job.id, ["queued"], "running", commit=False):
                continue
            conn.execute(
                "UPDATE auto_reply_jobs SET attempts = attempts + 1, started_at = ?, "
                "submitted_at = NULL, submission_sha256 = NULL WHERE id = ?",
                (now_iso, job.id),
            )
            transition(conn, "messages", job.message_id, ["queued"], "running", commit=False)
            claimed = get_job(conn, job.id)
            assert claimed is not None
            return claimed
        return None

    return _run_write_txn(db, _txn)


# ---------------------------------------------------------------- G5 제출


def mark_submitted(db: Database, *, job_id: int, body_sha256: str, now: datetime) -> bool:
    """G5(§7.9): `UPDATE … SET submitted_at, submission_sha256 WHERE id=? AND status='running'
    AND submitted_at IS NULL`의 rowcount가 1일 때만 True. 본문은 DB에 넣지 않는다(해시만)."""
    if not isinstance(body_sha256, str) or not _SHA256_RE.match(body_sha256):
        raise PolicyError("본문 해시 형식이 올바르지 않습니다")

    def _txn(conn: sqlite3.Connection) -> bool:
        cursor = conn.execute(
            "UPDATE auto_reply_jobs SET submitted_at = ?, submission_sha256 = ? "
            "WHERE id = ? AND kind = 'auto_reply' AND status = 'running' AND submitted_at IS NULL",
            (_iso(now), body_sha256, job_id),
        )
        return cursor.rowcount == 1

    return _run_write_txn(db, _txn)


# ---------------------------------------------------------------- 러너 종료 경로(G7 이전)


def cancel_running_job(db: Database, *, job_id: int, reason: str, now: datetime) -> bool:
    """running 잡을 초안 없이 cancelled로(설정 격리 폴백3, 규칙 없음 등)."""

    def _txn(conn: sqlite3.Connection) -> bool:
        job = get_job(conn, job_id)
        if job is None or job.status != "running":
            return False
        return _end_job_cancelled(
            conn, job, from_states=("running",), reason=reason, now_iso=_iso(now)
        )

    return _run_write_txn(db, _txn)


def fail_running_job(
    db: Database,
    *,
    job_id: int,
    error_class: str,
    reason: str,
    now: datetime,
    discard: bool = False,
    pause_security: bool = False,
    validation: dict[str, Any] | None = None,
) -> bool:
    """running 잡을 failed로(G6 사후 검증 실패, 타임아웃, CLI 없음 등). 초안은 만들지 않는다."""
    if error_class not in ("transient", "permanent", "auth", "policy", "security"):
        raise ValueError(f"알 수 없는 error_class: {error_class}")

    def _txn(conn: sqlite3.Connection) -> bool:
        job = get_job(conn, job_id)
        if job is None or job.status != "running":
            return False
        return _end_job_failed(
            conn,
            job,
            error_class=error_class,
            reason=reason,
            discard=discard,
            pause_security=pause_security,
            now_iso=_iso(now),
            validation=validation,
        )

    return _run_write_txn(db, _txn)


# ---------------------------------------------------------------- G7 결과 확정


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    """G7 결과. outcome: drafted / skipped / cancelled / discarded / rejected / failed."""

    outcome: str
    draft_id: int | None = None
    reason_code: str | None = None


class _VerifierFailure(Exception):
    """판정기 예외·반환 타입 오류·바인딩 불일치. 트랜잭션을 롤백하고 잡을 failed로 둔다."""

    def __init__(self, reason: str, *, security: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.security = security


def _preflight_summary(preflight: PreflightResult) -> dict[str, Any]:
    return {
        "claude_executed": preflight.claude_executed,
        "isolation_mode": preflight.isolation_mode,
        "jobdir_acl": preflight.jobdir_acl,
        "jobdir_fresh": preflight.jobdir_fresh,
        "ancestor_clean": preflight.ancestor_clean,
        "user_settings_unchanged": preflight.user_settings_unchanged,
        "unexpected_files": list(preflight.unexpected_files),
        "submit_count": preflight.submit_count,
        "exit_code": preflight.exit_code,
    }


class AutoReplyRepository:
    """G7 권위 판정 + 결과 확정(§7.11).

    판정기(`core.ports.AutoSendVerifier`)는 생성자에서 **한 번** 주입한다. 프로덕션 구현은
    `rules/autosend_verify.py` 하나뿐이고 `AutoReplyRepository(` 생성은 app.py에만 있다(아키텍처
    테스트). `commit_autoreply_result`에는 판정 함수 인자가 없다(§7.10 I-1).
    """

    def __init__(self, db: Database, *, verifier: AutoSendVerifier) -> None:
        if verifier is None or not callable(getattr(verifier, "verify", None)):
            raise TypeError("AutoReplyRepository에는 verify()를 가진 판정기가 필요합니다")
        self._db = db
        self._verifier = verifier

    def commit_autoreply_result(
        self,
        *,
        job_id: int,
        proposed: ProposedReply,
        guard: GuardResult,
        preflight: PreflightResult,
        now: datetime,
    ) -> CommitOutcome:
        """G7: writer 트랜잭션 **하나**에서 잡 CAS → 본문 해시 대조 → 판정 → 초안 INSERT·잡 done·
        메일 상태·로그를 한 번에 커밋한다. 판정기 예외는 전부 롤백하고 잡을 failed로 둔다.

        **Phase B**: 판정이 `draft`이면 `status='draft'` 초안만 만든다(approved_by 없음). 판정 값에
        자동발송이 없으므로 outbox로 가는 경로가 구조적으로 없다.
        """
        now_iso = _iso(now)
        body_hash = sha256_text(proposed.body_text)

        def _txn(conn: sqlite3.Connection) -> CommitOutcome:
            job = get_job(conn, job_id)
            # ① 잡 CAS: running이고 제출 기록이 있어야 한다. 긴급정지·끄기·사용자 취소로 이미
            #    cancelled된 잡이면 초안 없이 끝난다(M-C 1, Spinoza 확인사항 5).
            if (
                job is None
                or job.kind != "auto_reply"
                or job.status != "running"
                or job.submitted_at is None
            ):
                return CommitOutcome(outcome="rejected", reason_code="job_not_running")
            # ⑦ 본문 동일성(저장소 쪽 1차): 트랜잭션 안에서 읽은 submission_sha256과
            #    INSERT할 본문의 해시, GuardResult의 해시가 모두 같아야 한다(M-C 2·3).
            stored = job.submission_sha256 or ""
            if not (
                hmac.compare_digest(body_hash, stored)
                and hmac.compare_digest(guard.body_sha256, stored)
            ):
                _end_job_failed(
                    conn,
                    job,
                    error_class="security",
                    reason="body_hash_mismatch",
                    discard=True,
                    pause_security=True,
                    now_iso=now_iso,
                )
                return CommitOutcome(outcome="discarded", reason_code="body_hash_mismatch")
            try:
                verdict = self._verifier.verify(conn, job, guard=guard, preflight=preflight)
            except Exception as exc:  # noqa: BLE001 — 판정기 오류는 롤백 후 failed(아래)
                raise _VerifierFailure(
                    f"verifier_error:{type(exc).__name__}", security=False
                ) from exc
            if not isinstance(verdict, AutoSendVerdict):
                raise _VerifierFailure("verifier_bad_type", security=True)
            if (
                verdict.job_id != job.id
                or verdict.generation != job.enable_gen
                or verdict.rule_version != job.rule_version
            ):
                raise _VerifierFailure("verdict_binding_mismatch", security=True)

            if verdict.decision == "cancel":
                _end_job_cancelled(
                    conn,
                    job,
                    from_states=("running",),
                    reason=verdict.reason_code or "gate",
                    now_iso=now_iso,
                )
                return CommitOutcome(outcome="cancelled", reason_code=verdict.reason_code)
            if verdict.decision == "discard":
                _end_job_failed(
                    conn,
                    job,
                    error_class="security",
                    reason=verdict.reason_code or "security",
                    discard=True,
                    pause_security=True,
                    now_iso=now_iso,
                    validation={"preflight": _preflight_summary(preflight)},
                )
                return CommitOutcome(outcome="discarded", reason_code=verdict.reason_code)
            if verdict.decision != "draft":
                raise _VerifierFailure("verdict_unknown_decision", security=True)
            return self._commit_draft(conn, job, proposed, guard, preflight, verdict, now_iso)

        try:
            return _run_write_txn(self._db, _txn)
        except _VerifierFailure as failure:
            logger.error("자동회신 G7 판정 실패: job_id=%s reason=%s", job_id, failure.reason)
            fail_running_job(
                self._db,
                job_id=job_id,
                error_class="security" if failure.security else "permanent",
                reason=failure.reason,
                now=now,
                discard=True,
                pause_security=failure.security,
            )
            return CommitOutcome(outcome="failed", reason_code=failure.reason)

    @staticmethod
    def _commit_draft(
        conn: sqlite3.Connection,
        job: AutoReplyJobRow,
        proposed: ProposedReply,
        guard: GuardResult,
        preflight: PreflightResult,
        verdict: AutoSendVerdict,
        now_iso: str,
    ) -> CommitOutcome:
        message = conn.execute(
            "SELECT account_id, from_addr_norm, thread_key FROM messages WHERE id = ?",
            (job.message_id,),
        ).fetchone()
        tools = preflight.tools_used if preflight.claude_executed else None
        common_log: dict[str, Any] = {
            "ts": now_iso,
            "message_id": job.message_id,
            "job_id": job.id,
            "rule_id": job.rule_id,
            "rule_version": job.rule_version,
            "sender_addr_norm": job.sender_addr_norm,
            "claude_turns": preflight.turns,
            "tools_used": tools,
            "thread_key": message["thread_key"] if message is not None else None,
        }

        if proposed.decision == "skip":
            if not transition(conn, "auto_reply_jobs", job.id, ["running"], "done", commit=False):
                raise PolicyError("잡 전이 경합")
            conn.execute(
                "UPDATE auto_reply_jobs SET outcome = 'skipped', finished_at = ? WHERE id = ?",
                (now_iso, job.id),
            )
            transition(conn, "messages", job.message_id, ["running"], "skipped", commit=False)
            _insert_log(
                conn,
                outcome="skipped",
                reason_code="claude_skip",
                validation={"reason_len": len(proposed.reason or "")},
                **common_log,
            )
            return CommitOutcome(outcome="skipped", reason_code="claude_skip")

        # ⑤ 수신자 = 메일 From addr-spec 하나(트랜잭션 안에서 다시 읽은 정규화 값과 대조).
        if (
            message is None
            or not message["from_addr_norm"]
            or proposed.to_addr_norm != message["from_addr_norm"]
        ):
            _end_job_failed(
                conn,
                job,
                error_class="security",
                reason="recipient_mismatch",
                discard=True,
                pause_security=True,
                now_iso=now_iso,
            )
            return CommitOutcome(outcome="discarded", reason_code="recipient_mismatch")

        # approved_by·approved_at·outbox_expires_at은 쓰지 않는다(NULL) — 사람이 검토할 초안이다.
        cursor = conn.execute(
            "INSERT INTO drafts (account_id, kind, source_message_id, to_addrs, cc_addrs, "
            "bcc_addrs, subject, body_text, attachments_json, origin, job_id, status, "
            "created_at, updated_at) "
            "VALUES (?, 'reply', ?, ?, '[]', '[]', ?, ?, '[]', 'autoreply', ?, 'draft', ?, ?)",
            (
                int(message["account_id"]),
                job.message_id,
                json.dumps([proposed.to_addr], ensure_ascii=False),
                proposed.subject,
                proposed.body_text,
                job.id,
                now_iso,
                now_iso,
            ),
        )
        assert cursor.lastrowid is not None
        draft_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO draft_recipients (draft_id, kind, addr_norm, domain_norm) "
            "VALUES (?, 'to', ?, ?)",
            (draft_id, proposed.to_addr_norm, proposed.to_domain_norm),
        )
        # 잡 CAS(①의 마지막 전이): 같은 트랜잭션에서 running·submitted를 이미 확인했다.
        if not transition(conn, "auto_reply_jobs", job.id, ["running"], "done", commit=False):
            raise PolicyError("잡 전이 경합")
        conn.execute(
            "UPDATE auto_reply_jobs SET outcome = 'drafted', result_draft_id = ?, finished_at = ? "
            "WHERE id = ?",
            (draft_id, now_iso, job.id),
        )
        transition(conn, "messages", job.message_id, ["running"], "drafted", commit=False)
        _insert_log(
            conn,
            outcome="drafted",
            reason_code=verdict.reason_code,
            recipient_basis="from_header",
            validation={
                "guard_ok": guard.ok,
                "guard_findings": list(guard.findings),
                "preflight": _preflight_summary(preflight),
            },
            body_len=len(proposed.body_text),
            body_sha256=job.submission_sha256,
            recipient_norm=proposed.to_addr_norm,
            **common_log,
        )
        return CommitOutcome(outcome="drafted", draft_id=draft_id, reason_code=verdict.reason_code)


# ---------------------------------------------------------------- UI 조회


GUARD_UNKNOWN = "guard_unknown"


def get_draft_guard_findings(conn: sqlite3.Connection, draft_id: int) -> tuple[str, ...] | None:
    """자동회신 초안이면 그 잡의 G7 출력 가드 결과(findings), 아니면 None (§7.5 작성 창 배너).

    가드 결과는 G7이 auto_reply_log(outcome=drafted)의 validation_json에 남긴 값이다(본문 평문은
    저장하지 않으므로 다시 계산하지 않는다). 기록을 찾지 못하거나 읽을 수 없으면
    `(GUARD_UNKNOWN,)`을 돌려준다 — 경고를 숨기는 쪽으로 실패하지 않는다.
    """
    found = _autoreply_draft_validation(conn, draft_id)
    if found is None:
        return None
    validation = found[0]
    findings = validation.get("guard_findings") if isinstance(validation, dict) else None
    if not isinstance(findings, list):
        return (GUARD_UNKNOWN,)
    return tuple(str(item) for item in findings if isinstance(item, str))


def _autoreply_draft_validation(
    conn: sqlite3.Connection, draft_id: int
) -> tuple[dict[str, Any] | None] | None:
    """자동회신 초안이 아니면 None, 맞으면 (G7 drafted 로그의 validation dict 또는 None,)."""
    row = conn.execute("SELECT origin, job_id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None or row["origin"] != "autoreply":
        return None
    if row["job_id"] is None:
        return (None,)
    log = conn.execute(
        "SELECT validation_json FROM auto_reply_log WHERE job_id = ? AND outcome = 'drafted' "
        "ORDER BY id DESC LIMIT 1",
        (int(row["job_id"]),),
    ).fetchone()
    try:
        validation = json.loads(log["validation_json"]) if log is not None else None
    except (TypeError, ValueError):
        validation = None
    return (validation if isinstance(validation, dict) else None,)


def get_autoreply_draft_source(conn: sqlite3.Connection, draft_id: int) -> str | None:
    """자동회신 초안이 어떻게 만들어졌는지(작성 창 안내 문구용, 데카르트 33번 I-4).

    - `None`: 자동회신 초안이 아님(사용자 작성·MCP 초안 등)
    - `"claude"`: claude를 실행해 만든 초안
    - `"fixed_template"`: 고정 템플릿 초안(claude 미실행)
    - `"unknown"`: 자동회신 초안이지만 G7 기록을 읽을 수 없음
    근거는 G7이 drafted 로그에 남긴 `preflight.claude_executed`다(규칙은 나중에 바뀔 수 있어
    규칙의 reply_mode로 추정하지 않는다).
    """
    found = _autoreply_draft_validation(conn, draft_id)
    if found is None:
        return None
    validation = found[0]
    preflight = validation.get("preflight") if validation is not None else None
    executed = preflight.get("claude_executed") if isinstance(preflight, dict) else None
    if executed is True:
        return "claude"
    if executed is False:
        return "fixed_template"
    return "unknown"


@dataclass(frozen=True, slots=True)
class JobListRow:
    """Claude/MCP 패널 "잡" 목록 한 줄(§11.5). 제목·주소는 비신뢰 문자열 — UI는 평문으로만 쓴다."""

    id: int
    kind: str
    status: str
    outcome: str | None
    planned_action: str
    downgrade_reason: str | None
    error: str | None
    created_at: str
    finished_at: str | None
    message_id: int
    subject: str | None
    from_addr: str | None
    rule_name: str | None
    result_draft_id: int | None


def list_recent_jobs(conn: sqlite3.Connection, *, limit: int = 100) -> list[JobListRow]:
    rows = conn.execute(
        "SELECT j.id, j.kind, j.status, j.outcome, j.planned_action, j.downgrade_reason, j.error, "
        "j.created_at, j.finished_at, j.message_id, j.result_draft_id, m.subject, m.from_addr, "
        "r.name AS rule_name "
        "FROM auto_reply_jobs j LEFT JOIN messages m ON m.id = j.message_id "
        "LEFT JOIN rules r ON r.id = j.rule_id ORDER BY j.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        JobListRow(
            id=int(r["id"]),
            kind=r["kind"],
            status=r["status"],
            outcome=r["outcome"],
            planned_action=r["planned_action"],
            downgrade_reason=r["downgrade_reason"],
            error=r["error"],
            created_at=r["created_at"],
            finished_at=r["finished_at"],
            message_id=int(r["message_id"]),
            subject=r["subject"],
            from_addr=r["from_addr"],
            rule_name=r["rule_name"],
            result_draft_id=r["result_draft_id"],
        )
        for r in rows
    ]


def count_jobs_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM auto_reply_jobs WHERE kind = 'auto_reply' GROUP BY status"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}
