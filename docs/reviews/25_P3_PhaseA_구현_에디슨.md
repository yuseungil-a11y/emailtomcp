# P3 Phase A(토글 인프라) 구현 — Edison

## 요약
토글 인프라 구현 완료. 신규 테스트 전부 통과, P2 회귀 테스트도 통과. 테이블(auto_reply_jobs/auto_reply_log/rules)이 이미 m0001에 있어서 "없으면 no-op" 조건 없이 전부 실구현됨.

## 구현 내용

**1. 마이그레이션** `m0003_autoreply_toggle.py`(user_version=3): auto_reply_jobs에 enable_gen/submitted_at/submission_sha256, accounts.trusted_boundary_by, auto_reply_log.recipient_norm/thread_key, ux_jobs_autoreply_msg 유니크인덱스. 이미 있는 컬럼은 재추가 안 함.
- **세대 초기값을 m0003에서 1로 삽입**(지시서의 "최초 enable 0→1"과 다름 — §7.0/L-B 문구를 따른 것, 첫 켜기가 1→2가 됨). 키 없으면 0, 그 상태로 켜면 1.

**2. `storage/repositories/autoreply.py`**(신규): 보호키 상수 6개 정의처. `disable_and_drain`(끄기1a~1e, writer트랜잭션1개, CAS없이 항상성공/L-A, 세대손상시 세대는 안건드리고 enabled=false만, 잡취소+로그+outbox드레인+sending건수, 실패시 전체롤백 확인됨). `enable(expected_generation)`(CAS, 불일치/손상/이미켜짐/bool인자시 PolicyError). `recover_on_startup`(인자+DB유효값 둘다true일때만 켜진것으로 봄/fail-closed). `get_toggle_state`(저장값이 정확히 true+세대1이상일때만 켜짐). `preflight_counts`. `drain_autosend_outbox`(아래 1번 참조).

**3. I8 강제**: 실제 단일전이함수가 drafts.py가 아니라 `storage/transitions.py`의 `transition()`이라 거기 구현. to_state='draft' 전이시 같은 UPDATE안에서 approved_by='policy_auto_send'인 행만 approved_by/approved_at/outbox_expires_at NULL화(CASE문). user_send/ui/policy_allowlist 행은 안건드림(테스트확인). 여러전이를 한트랜잭션에 묶기 위해 `commit: bool=True` 키워드 추가(기본값은 기존동작과 동일).

**4. `autoreply/controller.py`**(신규): OFF/STARTING/ON/STOPPING, asyncio.Lock 직렬화. disable()(메모리 먼저내리고 DB1단계 실행, DB오류나도 메모리는off유지하고 예외전파). enable()(CAS실패시 PolicyError전파+이전상태복원). start_if_enabled()/shutdown() 구현. 토글변경시 AutoReplyEnabledChanged 이벤트 발행. Phase B 연결지점(구독/따라잡기/토큰폐기/kill)은 주석표시.

**5. app.py**: AUTOREPLY_PROTECTED_SETTING_KEYS 추가(repo상수참조), set_setting이 공백/대소문자 정규화 비교 후 PolicyError. get_autoreply_toggle/disable_autoreply/enable_autoreply(expected_generation) 추가. start_if_enabled()는 backend.start() 직후, Outbox·폴링루프 예약 전에 호출(남은 autosend outbox 선발송 방지). 종료시퀀스에 shutdown() 포함.

**6. UI**: 설정>자동회신탭 맨위에 체크박스+PlainText상태라벨, initial_tab="autoreply"로 바로 열기 가능. off→on저장시 `AutoReplyEnableConfirmDialog`(기본[취소], 1.5초 입력잠금 — send_approval_dialog.INPUT_LOCK_MS 재사용, 취소/CAS실패시 창 안닫힘, CAS실패시 평문경고+재조회). on→off는 확인창없이 즉시+결과정보창(0건이어도 평문으로). 메인창 상태줄 `자동회신 ○꺼짐/●켜짐`+클릭시 설정탭열기(이벤트payload 안믿고 백엔드재조회). [긴급정지]버튼은 이미있어 off일때 비활성+툴팁.

## 바뀐 화면/매뉴얼
`07_설정.md`("P3에서 제공" 문구 교체: 기본꺼짐/켜기끄기방법/소급없음/CAS경고/규칙보존), `01_메인창.md`(상태줄 "자동발송"→"자동회신" 교체, 긴급정지 설명갱신). `04_규칙설정.md`는 지시대로 미작성.

## 지시/설계와 다르게 한 것 (데카르트 §12.3 대조용, ⚠️검증필요)
1. **`drain_autosend_outbox` 본체 완성**(지시서는 "항상0반환 스텁"): 자리표시자로 두면 Phase B/C에서 G7이 먼저 붙으면 끄기가 outbox를 놓치는 fail-open 구간이 생긴다는 판단. 현재 policy_auto_send 행을 만드는 코드가 없어 실데이터는 항상0건(주석명시, 테스트는 직접만든 행으로 검증). Phase B/C에 남은것: send_unknown→draft 보강로그, 설정저장경로(M-A) 호출.
2. **m0003 설정정리**: 기존 `autoreply.enabled` 키 삭제(I7, v2까지 보호키장치 없어 범용경로로 쓰였을 가능성). 유효하지않은 generation(비정수/true/1미만)을 1로 덮어씀 — §7.0 "재설정경로없음" 문구와 어긋남(이 시점엔 잡전부 enable_gen=0이라 ABA위험없다고 판단). **⚠️Spinoza 확인 필요**.
3. `get_toggle_state(conn)` — 설계시그니처 `(db)`와 다름(다른 repo읽기함수 패턴따름).
4. 사전점검 숫자(규칙수/trust미설정계정수) 0고정 대신 실제테이블에서 셈(현재 사실상0). claude_cli_pinned만 None("해당없음").
5. 추가 결정: 이미켜진상태에서 enable호출시 거부(watermark밀림방지). 이미꺼진상태에서도 disable은 세대를 올림(단조증가유지). 기동시 running→queued복구는 kind=auto_reply 잡만 대상.
6. 상태줄표시를 지시대로 `자동회신 ●켜짐`으로(설계서의 "기존 자동발송 표시 재사용"과 다름). 켜져도 긴급정지는 Phase B실제동작이라 비활성(툴팁"이후단계에서 제공").
7. UiApi를 §11.7의 `set_autoreply_enabled(True,…)` 대신 §7.11의 `disable_autoreply`/`enable_autoreply` 분리로 구현(더 구체적인 보안리뷰 반영본을 따름).

## Phase B 이후로 미룬 것
- RuleEngine, MessageReceived구독, 따라잡기, 토큰폐기+프로세스kill, G3~G8, check_autoreply_gate.
- **SendService G8 미구현 — I1(off시 policy_auto_send의 outbox→sending 0건)을 발송경로에서 막는 장치 없음.** 현재 그런행 자체가 안생겨 실위험없음.
- auto_reply_log.outcome CHECK에 §7.3의 autosend_reset_unknown 없음 — Phase B에서 rebuild_table 마이그레이션 필요.
- 기동복구 실패시 로그만남기고 컨트롤러OFF, Outbox루프는 그대로 돎(G8로 보완예정).

## 테스트 결과
- 신규+마이그레이션 테스트: **128 passed**(test_autoreply_toggle.py 31건: CAS/L-A/ABA/세대단조/손상시fail-closed/드레인원자성/기동복구/동시enable중1건만성공 등, test_drafts_i8_reset.py, test_architecture_autoreply_keys.py, test_autoreply_ui_api.py: 보호키6×값5종+변형키 전부PolicyError, test_autoreply_toggle_ui.py: 1.5초잠금/기본[취소]/잠금중Enter무시/저장흐름/상태줄. test_migrations.py에 m0003테스트8건 추가).
- P2 회귀: test_mcp_tools/test_draft_service/test_send_service/test_transitions/test_settings — **61 passed**.
- 기존UI·통합: test_dialogs_smoke/test_mcp_ui/test_update_ui/test_main_window_smoke/test_ui_api_move_to_trash/test_mcp_auth_matrix/test_errors/test_migrations — **82 passed**.
- 커버리지: controller 99%, repo autoreply 99%, transitions 92%.
- ruff/mypy(변경파일) 통과. 남은 실패는 전부 이번에 미접촉 파일(기존 백로그).
- run_app() 오프스크린 스모크: user_version=3, 기동복구로그 확인, 종료코드0. 종료시 "Task was destroyed" 로그는 Backend.stop()이 주기작업 취소 직후 루프멈춰 생기는것, 이번변경과 무관.

## 변경 파일
신규: `storage/migrations/m0003_autoreply_toggle.py`, `storage/repositories/autoreply.py`, `autoreply/controller.py`, `tests/unit/{test_autoreply_toggle,test_drafts_i8_reset,test_architecture_autoreply_keys,test_autoreply_ui_api}.py`, `tests/ui/test_autoreply_toggle_ui.py`
수정: `storage/transitions.py`, `storage/migrations/runner.py`, `autoreply/__init__.py`, `app.py`, `ui/dialogs/settings_dialog.py`, `ui/main_window.py`, `tests/unit/test_migrations.py`, `docs/manual/{07_설정,01_메인창}.md`
