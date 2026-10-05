# P3 Phase B(규칙엔진+잡실행, 초안전용) 구현 — Edison

> 참고: 이 보고서 전달 과정에서 하네스가 "bypass-permissions 패턴 매칭" 경고를 띄웠음. 내용 확인 결과 Edison이 설정격리 폴백3 판정 조건을 설명하며 "hooks·활성 플러그인·MCP 외 allow·bypassPermissions·apiKeyHelper·위험 env" 중 사용자의 claude 설정에 `bypassPermissions`가 켜져 있으면 안전을 위해 잡을 취소한다는 **방어 로직 설명**일 뿐, 실제 권한상승 시도나 지시가 아님을 확인함(제우스 판단). 참고로만 남김, 추가조치 없음.

## 요약
규칙 저장소·편집UI, RuleEngine(G2·G3), 잡러너(G4→실행→G5→G6→G7), MCP잡토큰·J도구 구현완료. 끝까지 동작하는 경로: "메일수신→규칙매칭→claude -p(또는 고정템플릿)→**초안**". **자동발송은 열지 않음**(G7은 status='draft'로만 INSERT, approved_by에 자동발송값 쓰는 코드 없음).

신규테스트 160건+회귀306건 통과(10개 파일, 전체스위트는 미실행). ruff/mypy(신규모듈) 통과. 오프스크린 run_app() 스모크 정상(user_version=3, 기동복구, MCP시작/종료, 종료코드0).

## ⚠️ 먼저 알아야 할 것
1. **R1 실측보고서(docs/research/01_R1_실측.md) 없음** — 설계상 P3 진입게이트. 이 PC에 claude CLI가 없어 실제실행검증 못함. argv(--tools "", --setting-sources, dontAsk), stream-json스키마, mcp-config `${VAR}`확장은 설계서 가정대로 구현+미실측. 테스트는 가짜claude로 수행, 실제 MCP J도구 핸들러 호출로 G5까지는 실제경로 지남.
2. 의존성 google-re2·psutil 추가(pyproject+spec+.venv). 전역Python에도 실수로 설치됨(동작영향없음).
3. 기존테스트 3건 단언수정(의도된 동작변경): test_autoreply_toggle(첫켜기시 queue_state 미설정이면 running으로 초기화), test_autoreply_ui_api(claude_cli_pinned가 None대신bool), test_mcp_core(J도구가 잡스코프에 등록).

## auto_send 차단 — 7겹
1. 규칙저장검증+UiApi가 action=auto_send 거부("다음업데이트에서 제공예정")
2. RuleEngine이 auto_send매칭돼도 draft로 바꾸고 사유 `autosend_not_yet_supported` 기록
3. G3 create_autoreply_job이 planned_action≠draft면 PolicyError
4. G7 판정값타입 VerdictDecision이 {draft,cancel,discard}뿐(자동발송값 자체없음)
5. 프로덕션판정기 rules/autosend_verify.py는 통과해도 항상 draft 반환
6. G7 INSERT가 `'autoreply',?,'draft'`로 고정, approved_by 컬럼 미사용
7. 아키텍처테스트가 강제: 'policy_auto_send'리터럴위치, ApprovedBy.POLICY_AUTO_SEND참조위치, G3/G4/G5/G7 함수안에 자동발송값·outbox·approved_by 없음

## 구현내용 요약
- **규칙저장소/UI**: `storage/repositories/rules.py`(CRUD, version CAS, 사용/중지, 우선순위, 저장·중지·삭제시 drain_autosend_outbox(rule_id)호출/Phase B엔대상0건, 삭제시 진행중잡 먼저cancelled(rule_deleted)후FK끊고삭제). `rules/schema.py`, `rules/regex_safe.py`(RE2,200자,대소문자무시). `ui/dialogs/rule_editor_dialog.py`(목록화면 토글off배너+[설정에서켜기]+▲▼+미매칭동작, 편집화면 조건표+동작선택(auto_send비활성+툴팁)+응답방식+드라이런(claude미실행·DB쓰기없음·평문결과)).
- **RuleEngine**(`rules/engine.py`): §7.2 0~6단계, 컨트롤러 on/off에 구독/해제+1회따라잡기. `rules/loop_guard.py`(정적조건1~11). `rules/gate.py`(§7.11시그니처, 본체는 storage에 있고 재노출 — 아래 설계차이1 참조).
- **잡실행(G3~G7,draft전용)**: autoreply.py에 check_autoreply_gate, G3 create_autoreply_job(gate+메일당1잡+DB집계상한), G4 claim_next_job(gate+세대+queue_state), G5 mark_submitted(조건부UPDATE), G7 AutoReplyRepository(db,verifier=...)(잡CAS+트랜잭션안해시대조, 판정기예외/타입불일치시롤백+failed, 수신자=From확인), 큐일시정지/재개, 잡목록조회. `autoreply/`패키지: job_runner/claude_runner(argv·stream파서)/env_policy(allowlist)/job_dir(Windows보호DACL,ctypes,실측확인)/preflight/cli_locator(경로+sha256고정,cmd금지,npm은node+cli.js)/process_runner(subprocess+psutil)/prompts.
- **폴백3**: user설정에 hooks·활성플러그인·MCP외allow·bypassPermissions·apiKeyHelper·위험env 있거나 읽을수없으면 claude미실행하고 **잡을cancelled**로 종료(지시보다 보수적 — draft도 안만듦). 판정기에서 재확인.
- **MCP**: `mcp_server/job_tokens.py`(메모리전용저장소, `ejob.`접두사라우팅, 폴백조회없음). `tools_job.py`(get_job_message, submit_auto_reply — G5후즉시토큰폐기). `scopes.SCOPE_JOB`. `server.tools_for_principal`(주체종류 닫힌enum 먼저분기).
- **UI연동**: Claude/MCP패널 "잡"탭(목록평문,큐상태,[일시정지]/[재개] off시비활성). 메인창(규칙설정메뉴,상태줄"잡대기N",그리드"자동회신"열). 계정자동회신탭(auto_reply_enabled체크,authserv_id·boundary_by수동입력,전역off안내).

## 필수테스트 대응 (①~⑥ 전부 커버)
①auto_send여도draft: test_autoreply_jobs.py/test_autoreply_pipeline.py ②판정기는draft만: test_autoreply_jobs.py+test_verdict_decision_type_has_no_autosend_value ③자동발송값쓰는코드없음: test_architecture_autoreply_phase_b.py+기존test_architecture_autoreply_keys.py ④잡↔대화형토큰403: test_mcp_job_tokens.py ⑤폴백3이면cancelled: test_autoreply_pipeline.py 외6종 ⑥LoopGuard1~11: test_loop_guard.py(37건). 추가: G4/G5조건, 늦은제출(R-c), 끄기순서(DB커밋→구독해제→토큰폐기→kill→러너정지), I2(off면0회), CLI해시변경, G6실패5종, 출력가드, 사전점검, env allowlist.

## 설계와 다른 점 (⚠️데카르트 대조 필요, 14개)
1. gate 본체위치: storage/repositories/autoreply.py에 두고 rules/gate.py는 재노출(storage가 rules를 import못해서/§4.2, G3·G4가 같은함수 공유해야함). 잡생성상한도 같은이유로 storage에.
2. G3/G7 시그니처에 now(G3·G7), thread_key(G3) 키워드 추가.
3. 첫켜기시 queue_state: 값없을때만 running으로씀(기존값보존).
4. **G7 gate불통과는 초안없이 cancel**(설계 R-i·R-j대로) — 지시서의 "gate통과여부와 무관하게draft"는 auto_send관문(AuthGate등)을 뜻한다고 해석.
5. **폴백3은 draft도안만들고cancel**(지시대로 보수적) — R1미실측이라 isolation_mode는 항상user설정검사로정함. 판정조건도 hooks·플러그인외로 넓힘.
6. G1: messages.auto_headers에 LoopGuard용헤더+is_backfill만저장(AR·Received원자료는 Phase C). 원자료없는메일은 평가안함(fail-closed). is_backfill은 "폴더첫동기화"로판정(빈메일함첫메일도 backfill로잡히는한계, UIDVALIDITY재동기화감지 안함).
7. 규칙삭제는 진행중잡을 취소(설계는 G7에서draft로강등).
8. 우선순위변경 드래그대신▲▼버튼. auth조건은 Phase B에서 항상"맞지않음"으로평가.
9. **G6실패등급**: 허용외도구만security(+paused_security), 나머지는permanent로폐기(설계는모두security).
10. 제출본문전달: JobSubmitted이벤트대신 core포트JobSubmissionSink로(이벤트버스가UI까지퍼져서). JobFinished(job_id만) 신규발행.
11. ProcessRunner포트에 stdout_bytes·release추가. MCP서버꺼져있으면 러너가잡 안가져감.
12. 출력가드는 결과기록만(초안이라 강등없음). 작성창 경고배너 미구현.
13. 미매칭동작은 전용API로저장(범용set_setting아님). claude.접두사설정은 범용경로에서막음.
14. 계정의신뢰관련필드저장시도 드레인(M-A)호출(현재0건).

## 안전하게 미룬것
자동발송(G7 outbox진입, SendService G8), AuthGate(evaluate_auth/ar_presets/자동탐지), LoopGuard동적조건12~14/auto_send레이트리밋/회로차단/긴급정지, autosend_reset_unknown로그+CHECK마이그레이션, get_job_thread/get_job_instructions(stdin폴백)/submit_summary/수동요약초안잡, Transient재시도(30·120초)/AuthError시paused_auth, claude --version/auth status/[테스트], 금칙어·상한값설정UI, 작성창가드배너, 실제claude e2e(R1선행필요).

## 바뀐화면↔매뉴얼
규칙설정(신규)→04_규칙설정.md(신규). Claude/MCP패널잡탭→05_Claude_MCP패널.md. 계정자동회신탭→03_계정설정.md. 상태줄잡대기/그리드열/도구메뉴→01_메인창.md. 설정자동회신탭안내→07_설정.md. 목차→00_인덱스.md. manual_viewer.SECTION_FILES에04등록. 설계서§11.9표는 미수정.

## 변경 파일
신규소스: `core/autoreply_types.py`, `mail/loop_headers.py`, `storage/repositories/rules.py`, `rules/{engine,loop_guard,gate,schema,regex_safe,output_guard,autosend_verify}.py`, `autoreply/{job_runner,claude_runner,env_policy,job_dir,preflight,cli_locator,process_runner,prompts}.py`, `mcp_server/{job_tokens,tools_job}.py`, `ui/dialogs/rule_editor_dialog.py`
수정소스: `core/ports.py`, `config/paths.py`, `storage/repositories/{autoreply,accounts,messages}.py`, `mail/sync_service.py`, `autoreply/controller.py`, `mcp_server/{scopes,server,tool_base}.py`, `app.py`, `ui/main_window.py`, `ui/models/message_list_model.py`, `ui/dialogs/{mcp_panel,account_dialog,manual_viewer}.py`, `pyproject.toml`, `packaging/emailtomcp.spec`
신규테스트: `tests/unit/{_autoreply_helpers,test_loop_guard,test_rules_schema_repo,test_autoreply_jobs,test_rule_engine,test_autoreply_pipeline,test_architecture_autoreply_phase_b,test_autoreply_ui_api_phase_b,test_autoreply_runner_parts}.py`, `tests/integration/test_mcp_job_tokens.py`, `tests/ui/test_rule_editor_ui.py`
수정테스트: `tests/unit/{test_autoreply_toggle,test_autoreply_ui_api,test_mcp_core}.py`
매뉴얼: `docs/manual/04_규칙설정.md`(신규), 수정: `00_인덱스/01_메인창/03_계정설정/05_Claude_MCP패널/07_설정.md`

Edison 본인이 Spinoza 검토를 명시적으로 요청함(잡토큰, 주체분기, 판정기, DACL, env allowlist 등 보안관련 변경 多).
