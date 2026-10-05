# P3 Phase A(자동회신 토글 인프라) 보안검토 — Spinoza

## 판정: Go (조건부)
Critical 0, High 0, Medium 1(Phase B 착수 조건), Low 5. Phase A 구현만으로는 승인 없이 메일이 나가는 경로 없음. 관련 테스트(test_autoreply_toggle, test_drafts_i8_reset, test_architecture_autoreply_keys, test_autoreply_ui_api, test_migrations) 105 passed.

## 1. 가장 중요: m0003 세대 재설정이 ABA 방지를 깨는가
**문구상 L-B("재설정 경로 없음, 마이그레이션 포함") 위반이지만, 실제 ABA 발생 경로는 없음.** Low, 문서정정+방어코드 보강 권고.
- 덮어쓰기 대상(키없음/0/음수/비정수/true/JSON손상) 중 0·음수→1은 증가방향이라 문제없음. 문자열"7"/5.5 같은 손상값→1만 의미상 감소 가능.
- v2 DB 업그레이드 시: m0001 auto_reply_jobs엔 enable_gen 컬럼 자체가 없어 ADD COLUMN DEFAULT 0 순간 전부 0이 됨 — v2 세대값 가진 잡 존재불가, Edison 주장 맞음.
- 마이그레이션 재실행 불가: runner.py가 user_version 게이트, upgrade+set_user_version이 같은 BEGIN/COMMIT(실패시 함께롤백), 버전 역행 거부, user_version 쓰는 다른 코드 없음(grep확인). DB백업복구는 잡+세대 함께 복원되어 정합 유지.
- 유일한 예외: enable_gen 컬럼이 이미 존재하는 경우(개발중 수동추가) — `_add_column_if_missing`이 건너뜀. 그래도 2차 방벽: m0003이 autoreply.enabled 삭제 → 마이그레이션 직후 첫기동은 반드시 off → recover_on_startup이 Outbox루프 예약 전에 queued·running auto_reply잡 전부cancelled+policy_auto_send outbox 전부draft로 정리 → 켜기 전 옛 잡 생존 불가.

### 초기값 1 직접삽입(지시서는 "최초enable 0→1")
**문제없고 오히려 더 안전.** DESIGN §7.0 1207·1211행도 "m0003에서 1로 시작"이라 설계와 일치.
- off상태에서 generation=1이어도 유효enabled는 `저장값true ∧ 세대≥1`일때만 참(`_effective_enabled`), enabled키는 m0003에서 삭제됨 — 세대값만으로 켜지는 경로 없음.
- 첫켜기는 1→2 — 세대1은 "켜진적없는값"이 되어 enable_gen=1인 잡은 정상경로로 생길 수 없음.
- DESIGN.md 970행("기본0")과 1207행("초기값1") 불일치는 문서정정 필요(L-2).

## 2. L-A: 끄기는 CAS없이 항상성공 — **안전**
- disable_and_drain은 writer트랜잭션1개 안에서 enabled=false+세대+1+잡취소+outbox드레인, 예외시 전체롤백. controller의 asyncio.Lock+단일writer가 직렬화.
- 연속 끄기시 세대 2증가해도 off상태엔 "정상잡" 없어 오취소 없음, 단조증가 유지.
- 켜기/끄기 경합시 끄기가 항상 마지막쓰기로 이기고, 켜기는 오래된 expected_generation으로 CAS거부됨 — 의도대로.
- 세대값 손상시 enabled=false만 쓰고 세대는 안건드림, 이후 켜기는 거부(fail-closed).

## 3. I8 원자성 — **원자적**
`transitions.py`: status변경+3개 CASE초기화가 **한 UPDATE문** 안에 있고 SQLite는 SET식을 갱신전 행 기준으로 평가. drafts를 draft로 바꾸는 다른 직접UPDATE 없음(grep확인). drain의 last_error UPDATE도 같은 트랜잭션.
- L-3: I8 문구는 "pending_approval에도 policy_auto_send 없어야함"이나 지금은 to_state='draft'일때만 초기화 — Phase B에서 →pending_approval 전이 생기면 같은처리 필요.

## 4. 보호키 우회경로 — **없음**
범용쓰기경로는 `config.settings.set_setting` 하나(UiApi.set_setting에 보호키검사 있음, update/state.py는 update.*고정키만). settings 직접 INSERT/UPDATE/DELETE는 m0003과 repo/autoreply.py뿐. MCP서버는 get_setting만 주입, 쓰기도구 없음.
- L-4: 지금은 allowlist 차단이라 `autoreply.unmatched_action` 등 다른 autoreply.* 키는 범용경로로 쓸 수 있음 — Phase B에서 RuleEngine이 이 키들을 읽으면 `autoreply.` 접두사 전체를 기본거부(update.*처럼)로 전환 권고.
- L-5: m0003이 enabled만 삭제, v2시절 범용경로로 쓰였을 수 있는 queue_state/autosend_state/watermark/enabled_changed_at은 그대로 둠 — queue_state/autosend_state는 정규화 또는 삭제 권고(watermark는 enable시점 재기록되어 무해).

## 5. drain_autosend_outbox 공격면 — **안전, Phase B 대비 L-6 있음**
SQL 전부 바인딩파라미터, WHERE 고정(`approved_by='policy_auto_send' AND status='outbox'`) — 인젝션 여지 없고 사람승인 메일 안건드림, 되돌리는 방향이라 안전. 현재 호출처는 끄기+기동복구(둘다 None,None)뿐.
- L-6: rule_id 지정시 `LEFT JOIN ... j.rule_id=?`라 job_id NULL이거나 잡삭제된 policy_auto_send 행은 안 걸림 — M-A(규칙변경드레인)에서 누락(fail-open) 가능. G7이 job_id 항상 채우도록 보장하거나 고아행도 함께 드레인할 것.

## 6. "Phase A엔 자동발송 없다" 최종확인 — **지켜짐**
grep결과 policy_auto_send를 drafts에 쓰는곳 없음. approved_by는 user_send(app.py하드코딩)/UI/POLICY_ALLOWLIST(MCP approval) 세값만, 외부입력이 approved_by를 정하는 경로 없음. 잡생성/실행코드(G3/G4) 없고, 기동복구 requeue 처리할 러너도 없음 — SendService G8 없어도 대상행 자체가 없음.

### Medium(M-1): Phase B 착수 필수조건
1. **G8을 SendService에 넣기 전에 G7(policy_auto_send outbox생성)을 먼저 붙이면 안됨.** 지금 끄기/기동복구 타임아웃(15초) 실패해도 Outbox루프가 그대로 도는 이유는 "그런 행이 없다"는 사실뿐.
2. `controller.disable()`에서 DB오류나면 메모리는off지만 DB엔 enabled=true 잔존 → 다음기동시 다시ON("끈줄알았는데 켜져있음"). Phase B에서 G8이 DB값 기준판정하는지 확인으로 충분.
3. G4/G7/G8은 반드시 `enabled유효값 ∧ enable_gen==generation ∧ enable_gen≥1`을 같은 트랜잭션에서 판정(세대일치만 보면 안됨).

## Low 요약
- L-1: m0003 손상값 1정규화가 L-B 문구와 어긋남 — 정책문구 정정+MAX(enable_gen)+1 기준으로 변경+재사용방지장치.
- L-2: DESIGN.md 970행"기본0"과 1207행"초기값1" 불일치 — 문서정정.
- L-3: I8의 pending_approval 측 보장은 Phase B에서 처리.
- L-4: autoreply.* 접두사 전체 기본거부 전환 권고.
- L-5: v2잔존키 queue_state/autosend_state 정규화.
- L-6: rule_id 드레인에서 job_id NULL 행 누락.
- 참고(등급외): v2시절 매우큰 generation값이면 Phase B의 enable_gen INTEGER 삽입시 OverflowError(fail-closed 방향이나 m0003서 상한검사 권고).

## 관련 파일
`storage/migrations/{m0003_autoreply_toggle,runner}.py`, `storage/repositories/autoreply.py`, `storage/transitions.py`, `autoreply/controller.py`, `app.py`(130-139,853-862,1520-1530행), `config/settings.py`, `DESIGN.md`(970,1207-1211행)
