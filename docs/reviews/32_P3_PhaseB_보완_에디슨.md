# P3 Phase B 보완 — Edison

## 요약
필수항목6건(H-1, Spinoza M-1·M-2, 데카르트 M-1·M-3·M-4)+Low3건(L-4, Spinoza L-1, 데카르트 L-1문서) 전부반영. 지정범위 345 passed. 범위조정 3곳 있음(아래).

## 필수항목

**H-1(CLAUDE_CONFIG_DIR불일치)** — 권고(b)안 적용. 점검대상경로를 자식env와 같은Mapping에서계산(자식홈키 Windows=USERPROFILE/그외=HOME 아래.claude, 원본·자식 어느쪽계산해도동일값). 원본env에 CLAUDE_CONFIG_DIR있으면(대소문자무관) `unsupported_env:CLAUDE_CONFIG_DIR`사유로fallback3+자식env에서도무조건삭제. 러너가 같은env_source를 사전점검+build_child_env에함께전달. 회귀테스트6건.

**Spinoza M-1(denylist→allowlist)**: 무조건허용키 8개뿐($schema,cleanupPeriodDays,includeCoAuthoredBy,autoUpdatesChannel,spinnerTipsEnabled,alwaysThinkingEnabled,feedbackSurveyState,disableAllHooks). 그외최상위키 하나라도있으면fallback3. 조건부허용: hooks/env(비어있을때만), enabledPlugins(전부false), permissions(allow는mcp__만/deny·ask·disableBypassPermissionsMode허용/defaultMode는default·plan·dontAsk만). managed settings(Windows/macOS/Linux경로)도 같은allowlist검사, managed-mcp.json있으면fallback3. ~/.claude/CLAUDE.md도해시대상. 회귀테스트: 노출13종+managed4종+무해2종.

**Spinoza M-2(사후점검강제)**: G7판정기④연결(claude실행잡만). user_settings_unchanged=False→discard(preflight_settings_changed,security,paused_security). unexpected_files있음→discard(preflight_unexpected_files). ancestor_clean=False→cancel. jobdir_acl≠owner_only→cancel. 둘다걸리면보안discard먼저. PreCheck에점검대상(config_dir,managed_dirs)기록, 사후해시도같은대상재계산. 회귀테스트: 판정기6건+파이프라인3건.

**데카르트M-1(backfill오판)**: m0004_folder_initial_sync 신규(folders.initial_sync_done 0/1). 업그레이드시 서버받은흔적있는폴더는1로. 판정은이표식만봄, 목록메일전부저장했을때만최초동기화완료기록(일부실패면다음도backfill,안전방향). 회귀테스트5건(데카르트재현시나리오, 빈메일함최초동기화, 부분실패, m0004 2건). 매뉴얼+DESIGN.md "폴더별최초동기화"로수정.

**데카르트M-3(작성창경고배너)**: PlainText배너추가, URL·금액·계좌등finding을한글라벨로표시. `autoreply_repo.get_draft_guard_findings`+UiApi `get_draft_guard_findings`로조회, G7이auto_reply_log에남긴가드결과읽음(기록없으면guard_unknown경고). 회귀테스트: UI4건+저장소1건.

**데카르트M-4(신뢰AR입력란)**: 두입력란비활성+"다음업데이트에서실측완료후제공" 안내. 저장시이값안보내 기존DB값불변(TRUST_FIELDS_EDITABLE=False). 기존UI테스트단언수정. 매뉴얼수정.

## Low항목
- **L-4**: `JobRunner.revalidate_current()`추가 — 실행중잡gate재확인, 불통과시DB취소커밋후abort_jobs(토큰폐기→kill). UiApi에서 규칙저장(수정)·중지·삭제와 계정auto_reply_enabled/enabled변경커밋후호출. 회귀테스트: 파이프라인4건(토큰폐기가kill보다먼저인지단언)+UiApi연결1건.
- **Spinoza L-1**: submit_count>1은 `security_submit_count`(security,paused_security)로(기존테스트기대값변경). 타임아웃경로서도stdout먼저파싱해허용외도구시도판정. overflow는 kill뒤잘린stdout이같은경로타서별도수정불필요.
- **데카르트L-1(문서)**: DESIGN R-i를cancel기준통일, preflight·fail-closed설명보강.

## 범위조정 3건 (⚠️확인필요)
1. **잡탭에[초안열기]버튼추가**: 자동회신초안(로컬drafts)을작성창으로여는UI경로가아예없었음(기존엔 MCP승인창[수정후발송]하나뿐) — M-3배너가실제로보일수없어 잡탭에버튼(더블클릭도가능)최소범위추가. 매뉴얼반영+UI테스트1건.
2. **출력가드금액탐지버그수정(범위외)**: M-3테스트중 "1,500,000원을","100만원입니다"처럼단위뒤조사붙으면금액놓치는것발견(정규식\b가한글끼리경계안만듦). 정규식수정+테스트4건(rules/output_guard.py). **판정로직변경이라Spinoza확인권고**.
3. **테스트헬퍼home_dir을실제홈기준으로변경**: M-2강제이후 가짜홈(tmp_path)쓰면 이PC의실제~/.claude때문에모든잡이ancestor사유로취소됨 — 운영과같은기준으로맞춤.

## 바뀐화면↔매뉴얼
작성창(경고배너)+잡탭([초안열기],사유라벨5종)→05_Claude_MCP패널.md. 계정설정>자동회신탭(신뢰AR입력비활성)→03_계정설정.md. backfill설명(화면변화없음,문구정정)→04_규칙설정.md.

## 테스트결과
지정범위+영향파일20개 **345 passed**: test_autoreply_pipeline54, test_autoreply_runner_parts24, test_rule_engine12, test_architecture_autoreply_phase_b13, test_mcp_job_tokens5, test_sync_service9, test_migrations33, test_autoreply_jobs26, test_autoreply_ui_api_phase_b5, test_dialogs_smoke18, test_rule_editor_ui15, 나머지회귀(mcp_ui,autoreply_toggle_ui,main_window_smoke,accounts_repo등)전부통과. ruff깨끗(변경파일기준), mypy 3건(app.py677-680)은기존·무관.

## R1실측때 확인필요한 부작용
- allowlist전환으로 일반사용자설정(model지정등)도fallback3됨 — Claude작성잡취소증가가능(가용성, L-5연장선).
- 실제claude가work/에파일이나.claude만들면 매번보안discard+paused_security — R1서확인필수.
- DESIGN의 G6실패등급문구(submit2회이상→security)는아직미갱신.

## 변경 파일
`autoreply/{preflight,env_policy,job_runner}.py`, `rules/{autosend_verify,output_guard}.py`, `mail/sync_service.py`, `storage/repositories/{folders,autoreply}.py`, `storage/migrations/{m0004_folder_initial_sync(신규),runner}.py`, `ui/dialogs/{compose_window,mcp_panel,account_dialog}.py`, `app.py`, `docs/{DESIGN.md,manual/{03_계정설정,04_규칙설정,05_Claude_MCP패널}.md}`, 테스트다수(보고서참조)
