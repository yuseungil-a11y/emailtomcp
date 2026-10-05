"""자동회신 유효 조건 gate (DESIGN.md §7.0 "유효 조건", §7.11) — P3 Phase B.

`check_autoreply_gate(conn, *, account_id, rule_id, rule_version, job_generation) -> GateResult`

G3(잡 생성)·G4(실행 시작)·G7(결과 확정)·G8(발송 직전, Phase C)이 **같은 함수 하나**를 공유해야
한다(P2에서 L1·L2 Host 값을 같은 함수로 만든 원칙, §7.10 L1·L2 행). 그런데 G3·G4는
storage(writer 트랜잭션)에 있고 §4.2상 storage는 rules를 import할 수 없다. 그래서 본체는
`storage/repositories/autoreply.py`에 두고, 이 모듈은 **그 함수 객체를 그대로** 다시 내보낸다
(복제·래핑 없음 — 아키텍처 테스트가 두 이름이 같은 객체임을 단언한다). 설계서의
"rules/gate.py에 둔다"와 위치만 다르고 의미는 같다.
"""

from __future__ import annotations

from emailtomcp.core.autoreply_types import GateResult
from emailtomcp.storage.repositories.autoreply import check_autoreply_gate

__all__ = ["GateResult", "check_autoreply_gate"]
