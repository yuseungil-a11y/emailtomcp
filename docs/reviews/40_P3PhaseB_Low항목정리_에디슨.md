# P3 Phase B Low 항목 정리 — Edison

요청한 7개 항목+DESIGN문구갱신 전부처리. 선택사항중Info-2처리, Info-1건너뜀. 지정테스트6개파일 203건 전부통과.

## 항목별 처리

**1. Spinoza L-2(드레인대상필드보강)** `storage/repositories/accounts.py`: `_TRUST_FIELDS`에 in_host·incoming_protocol·out_host·out_username추가. 드레인조건변경 — 이전엔"저장컬럼에신뢰필드포함되기만하면"드레인했으나, 이제**같은writer트랜잭션에서UPDATE직전값을읽어비교, 실제로바뀐경우에만드레인**(bool은DB저장0/1로맞춰비교, 계정창은빈값을이미None정규화해""/None오탐없음). 회귀테스트1건(같은값전체저장시outbox유지, 신규필드4개+signature+auto_reply_enabled 하나씩바꾸면각각draft로복귀확인).

**2. Spinoza L-5(판정기G6사실재확인)** `rules/autosend_verify.py`: G6값(도구목록,submit횟수,종료코드)은DB에따로저장안되고PreflightResult에만기록 — 판정기에두가지추가: ①G5제출기록재조회(트랜잭션안auto_reply_jobs직접재조회, status=running+submitted_at존재+해시일치확인, 어긋나면discard`g6_submission_missing`) ②PreflightResult기록값대조(tools_used⊆허용집합-고정템플릿은빈집합이어야함, submit_count==1, claude실행잡이면exit_code==0 — 어긋나면discard보안, 사유g6_tools_mismatch/g6_submit_count_mismatch/g6_exit_code_mismatch). 의존규칙상rules가autoreply import불가라 허용도구상수를`core/autoreply_types.py`의AUTOREPLY_DRAFT_ALLOWED_TOOLS로이동, env_policy.ALLOWED_TOOLS_AUTO_REPLY_DRAFT는이상수참조(러너·판정기同집합사용테스트로고정). 회귀테스트: 재확인매개변수9건+G5기록재조회1건+상수공유확인1건.

**3. Spinoza N-1(홈키없을때)** `autoreply/preflight.py`: child_home_dir(source)가None이면 `unsupported_env:home_missing`사유로fallback3(상수UNSUPPORTED_HOME_MISSING). 테스트헬퍼make_runner기본env에실제홈키추가(안그러면기본env쓰는claude경로테스트전부fallback3됨). 회귀테스트1건(빈env/SYSTEMROOT만/다른플랫폼홈키만→fallback3, 정상홈키있으면사유안붙음).

**4. Spinoza N-3(abort와보안판정순서)** `autoreply/job_runner.py`: `_run_claude`에서parse_stream+_security_failure를먼저실행후aborted확인으로순서변경. **순서만으론부족한경우발견**: 끄기1단계·revalidate_current는DB를먼저cancelled로커밋해 fail_running_job이무효해지고보안신호소실 — `_security_failure`가잡전이실패시 `pause_autosend_for_security`로paused_security를별도로걺추가보강. 회귀테스트4건(abort+허용외도구겹침/abort+submit2회겹침→둘다failed(security)+paused_security, 규칙중지로DB먼저cancelled된상태서허용외도구→잡은cancelled로남고paused_security걸림, 정상도구+abort→보안정지안걸림).

**5. Spinoza Info-3(claude_config_dir아키텍처테스트)** `test_architecture_autoreply_phase_b.py`: `test_production_never_overrides_claude_config_dir`추가 — JobRunner(생성은app.py에서만/그호출에claude_config_dir키워드나**펼침없음/run_pre_checks(config_dir=...)는job_runner.py만전달 3가지강제.

**6. 데카르트L-9(재수신메일재평가)**: 기존유니크인덱스(ux_jobs_autoreply_msg)는로컬messages.id기준이라재수신사본못막음확인. `messages_repo.has_autoreply_handled_copy(conn,account_id,message_id_hdr)`추가(기존ix_msg_msgid인덱스사용). `sync_service._fetch_and_store`가저장전이조회참이면해당사본을is_backfill=True로저장 — RuleEngine의기존backfill제외+catch_up경로가함께막아줘엔진코드는불변. **범위확장**: auto_reply_status IS NOT NULL뿐아니라backfill로빠졌던사본도"이미처리됨"으로간주(최초동기화로빠진과거메일을로컬만휴지통이동시다음동기화때일반메일로재평가되는같은류빈틈방지, 원하면NULL아님조건만남기도록되돌릴수있음). **알아둘점**: Message-ID없는메일은못막음. 공격자가이미평가된메일과같은Message-ID로보내면그메일자동회신억제가능(회신안하는방향이라가용성영향만). 회귀테스트4건(drafted/ignored사본막힘, NULL사본안막힘, 다른Message-ID무영향, backfill원본재수신사본도막힘 — 데카르트지적실제경로move_to_folder+remote_uid유지로재현).

**7. 데카르트I-4(고정템플릿초안문구)**: `autoreply_repo.get_autoreply_draft_source()`추가 — G7 drafted로그의preflight.claude_executed값으로claude/fixed_template/unknown/None(자동회신아님)구분(규칙변경가능성있어reply_mode로추정안함). UiApi에get_autoreply_draft_source추가. compose_window.py의open_existing이이값으로상단문구결정: MCP[수정후발송]초안은기존"Claude가만든초안"유지, 자동회신초안은"자동회신초안(Claude작성)"/"자동회신초안(고정템플릿)"/"자동회신초안"(unknown)중하나. 경고배너"자동회신(Claude)"표기도claude미실행초안에선"자동회신초안"으로. 회귀테스트UI5건+저장소2건.

**8. DESIGN.md갱신**: §7.9 G6표의"폐기,failed(security)"가실제구현과달라수정(실제론허용외도구+submit2회이상만security,나머지permanent). "G6실패등급"하위절신설(보안등급/일반등급/N-3/L-5). 함께반영: §7.9 M-A드레인대상에L-2필드+값비교조건, G1행에L-9재수신규칙, §7.11사전점검절에N-1·Info-3.

## 부수변경
잡탭사유라벨에신규사유코드4개(g6_*)추가(mcp_panel.py). 매뉴얼갱신(05_Claude_MCP패널.md: 신규사유/중지시보안정지/홈키없을때취소/초안문구구분, 04_규칙설정.md: 재수신메일재평가안함설명).

## 선택항목
Info-2(처리): `_run_claude`에서os.environ을dict(os.environ)으로한번스냅샷떠사전점검·build_child_env가같은값보게함. Info-1(건너뜀): 출력가드정규식성능 — re2전환/입력절단은판정로직변경이라Spinoza확인필요, 4000자상한있어시간대비이득적다판단. 지시대로R-2,R-3,N-2,N-5미접촉.

## 테스트결과
지정6파일: test_autoreply_pipeline60+test_autoreply_runner_parts57+test_sync_service14+test_migrations35+test_dialogs_smoke23+test_architecture_autoreply_phase_b14 = **203 passed**. 추가연관파일6개도전부통과(test_autoreply_jobs36,test_autoreply_toggle36,test_accounts_repo7,test_autoreply_ui_api_phase_b5,test_mcp_job_tokens5,test_rule_editor_ui15). ruff/mypy(변경파일)통과. 기존무관문제2건(sync_service.py:160 format, app.py:677-680 mypy)은범위밖.

## 후속확인권고(Edison 본인요청)
L-5의G7재확인, N-3의"이미cancelled된잡이면paused_security만걸기", L-9의Message-ID차단은보안·판정로직이라Spinoza·데카르트재확인권고.

## 변경 파일
`storage/repositories/{accounts,messages,autoreply}.py`, `rules/autosend_verify.py`, `core/autoreply_types.py`, `autoreply/{preflight,job_runner,env_policy}.py`, `mail/sync_service.py`, `ui/dialogs/{compose_window,mcp_panel}.py`, `app.py`, `docs/DESIGN.md`, `docs/manual/{04_규칙설정,05_Claude_MCP패널}.md`, 다수테스트파일
