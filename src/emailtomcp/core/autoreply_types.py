"""자동회신 파이프라인 공용 값 타입 (DESIGN.md §7.9, §7.11) — P3 Phase B.

여러 패키지(storage·rules·autoreply·mcp_server)가 **읽어야** 하는 결과 구조를 core에 둔다.
§4.2 의존 규칙상 storage는 rules/autoreply를 import할 수 없으므로, G7(`commit_autoreply_result`)이
판정 결과·가드 결과·사전점검 결과의 필드를 읽으려면 타입이 core에 있어야 한다.

**생성 위치는 제한한다**(아키텍처 테스트 `test_architecture_autoreply_phase_b.py`):
- `GuardResult`      — `rules/output_guard.py`만 생성한다.
- `PreflightResult`  — `autoreply/preflight.py`만 생성한다.
- `AutoSendVerdict`  — `rules/autosend_verify.py`만 생성한다.
그 밖의 타입(`GateResult`, `ProposedReply`, `AutoReplyJobRow`)은 제한이 없다.

Phase B 안전경계: `AutoSendVerdict.decision`에는 **자동발송 값이 없다**(`draft`/`cancel`/`discard`
세 가지뿐). 따라서 이번 단계의 G7은 구조적으로 outbox 행을 만들 수 없다. Phase C에서 G8
(SendService 발송 직전 재검증)이 들어온 뒤에 값을 추가한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# G7 판정(권위). Phase B에는 자동발송 값이 없다 — 위 모듈 docstring 참고.
VerdictDecision = Literal["draft", "cancel", "discard"]
# 사전점검 설정 격리 모드(§7.11 A8). `not_applicable`은 claude를 실행하지 않는 고정 템플릿 잡.
IsolationMode = Literal[
    "setting_sources", "config_dir", "user_settings_clean", "fallback3", "not_applicable"
]
JobDirAcl = Literal["owner_only", "failed"]
SubmitDecision = Literal["reply", "skip"]

# kind=auto_reply·draft 계획 잡이 쓸 수 있는 도구(`--allowedTools` 형식). 러너(G6, autoreply
# 패키지)와 판정기(G7, rules 패키지)가 같은 집합으로 대조해야 하는데 §4.2상 rules는 autoreply를
# import할 수 없어 core에 둔다(Spinoza 34번 L-5). `env_policy.ALLOWED_TOOLS_AUTO_REPLY_DRAFT`는
# 이 값을 그대로 쓴다. 서버 쪽 `scopes.SCOPE_TOOLS[SCOPE_JOB]`와 같은 집합이다.
AUTOREPLY_DRAFT_ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__emailtomcp__get_job_message",
    "mcp__emailtomcp__submit_auto_reply",
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """`check_autoreply_gate` 결과(§7.11). reason_code는 disabled, generation_mismatch,
    account_disabled, rule_disabled, rule_changed, rule_missing 중 하나."""

    ok: bool
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class AutoReplyJobRow:
    """writer 트랜잭션 안에서 읽은 잡 한 행(+ 메일의 계정 ID). G4·G7이 돌려주거나 판정에 쓴다."""

    id: int
    kind: str
    message_id: int
    account_id: int
    rule_id: int | None
    rule_version: int | None
    planned_action: str
    downgrade_reason: str | None
    status: str
    attempts: int
    enable_gen: int
    started_at: str | None
    submitted_at: str | None
    submission_sha256: str | None
    sender_addr_norm: str | None


@dataclass(frozen=True, slots=True)
class ProposedReply:
    """G7에 넘기는 제출 내용(메모리 본문). 러너가 만든다.

    - 수신자는 메일 From의 addr-spec 하나다(§7.5 C2). G7은 트랜잭션 안에서 메일의
      `from_addr_norm`과 `to_addr_norm`이 같은지 다시 확인한다.
    - `body_text`는 그대로 drafts.body_text에 들어간다(앱이 덧붙이는 블록 없음 — G7 ⑦
      "sha256(INSERT할 본문) = submission_sha256"을 지키기 위해서다).
    """

    decision: SubmitDecision
    body_text: str
    reason: str | None
    to_addr: str
    to_addr_norm: str
    to_domain_norm: str
    subject: str


@dataclass(frozen=True, slots=True)
class GuardResult:
    """출력 가드 결과(§7.5, §7.11). `rules/output_guard.check()`만 생성한다."""

    ok: bool
    findings: tuple[str, ...]
    body_sha256: str
    subject_sha256: str


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """잡 디렉터리·설정 격리 사전/사후 점검 결과(§7.11 A8). `autoreply/preflight.py`만 생성한다.

    job_id·attempt·시각에 묶인다. G7은 바인딩이 어긋나면 보안 실패로 본다.
    """

    job_id: int
    attempt: int
    pre_checked_at: str
    post_checked_at: str
    claude_executed: bool
    jobdir_fresh: bool
    jobdir_acl: JobDirAcl
    ancestor_clean: bool
    isolation_mode: IsolationMode
    user_settings_sha256: str
    user_settings_unchanged: bool
    unexpected_files: tuple[str, ...]
    tools_used: tuple[str, ...]
    submit_count: int
    turns: int | None
    exit_code: int | None
    verified_body_sha256: str | None


@dataclass(frozen=True, slots=True)
class AutoSendVerdict:
    """G7 권위 판정(§7.11 M-D). `rules/autosend_verify.py`만 생성한다.

    repo는 job_id·generation·rule_version이 트랜잭션에서 읽은 값과 다르면 거부한다.
    """

    job_id: int
    generation: int
    rule_version: int | None
    decision: VerdictDecision
    reason_code: str | None
