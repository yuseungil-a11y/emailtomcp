# P3 Phase B(규칙엔진+잡실행, 초안전용) 기능검증 — 데카르트

## 결론
- **코드수준: 조건부Go.** Critical/High 없음. "자동발송 절대안열림" 주장을 직접 공격시나리오로 확인.
- **출시게이트(v1.2.0): No-Go(보류).** 이유: ①R1실측없음(P3진입게이트, 승인자Spinoza+데카르트) ②작성창 출력가드 경고배너없음(M-3) ③backfill판정이 정상메일을 조용히빠뜨림(M-1).
- C1패턴과 동일. 고정템플릿경로는 R1무관하게 완결됨. "Claude작성"경로는 실제claude로 한번도 안돌려봄.

## 1. 테스트 재현
신규10+수정3+PhaseA회귀 18개파일: **341 passed**(12.7초, 신규10개만이면160건=보고와일치). P2/MCP회귀: 115 passed. transitions/settings/folder_tree: 10 passed.
기존테스트3건 단언수정은 **의도된동작변경 맞음(엄격해짐, 느슨해지지않음)**: toggle(첫켜기 queue_state=="running" 정확값+paused_user보존 신규테스트), ui_api(claude_cli_pinned is False, 이전None), mcp_core(J도구가 대화형스코프에없고 allowed_tools(["job"])이 정확히2도구).

## 2. 최우선: 자동발송차단 검증(직접 공격시나리오)
| 시도 | 결과 |
|---|---|
| raw SQL로 rules.action='auto_send'주입(UI검증우회)+trust필드채움+G2→G7전체실행(generated/fixed_template) | planned_action=draft, 사유autosend_not_yet_supported. draft생성, approved_by=NULL, outbox0건 |
| action에 send/AUTO_SEND/auto-send/빈값 | DB CHECK제약이 INSERT부터거부(8번째방어층) |
| create_autoreply_job(planned_action=auto_send/outbox/"Draft"/"draft ") | 전부PolicyError |
| verifier를 악성으로교체(decision=auto_send/outbox, duck타입, 예외) | 각각failed(discarded, verdict_unknown_decision/verifier_bad_type/verifier_error). 초안0,outbox0 |
| verifier가 gate무시+무조건draft반환 | 초안1건 status=draft로생김. outbox가는길없음 |
| G7직전 토글끄기(R-d) | cancelled, 초안0 |

판단: G7 INSERT가'draft'고정, `_commit_draft`외엔 drafts에쓰는경로없음. verifier를뭘로바꿔도최악은"gate무시한초안". verifier=주입+AutoReplyRepository(생성은 아키텍처테스트가app.py에만있도록강제(실제도 app.py:1759 한곳뿐). approved_by쓰는곳 Phase A와동일3곳, policy_auto_send쓰는코드 0.

## 3. 설계와다른점 14개 판정
| # | 판정 | 근거 |
|---|---|---|
| 1,2,3,7,8,10,13,14 | 타당 | (3: m0003이 키지워서 없으면G4가영원히잡안가져감, 기존값보존) |
| 4 | **타당** | §7.9 G7이 "gate·세대불통과:초안없이cancelled" / "불통과(정책):draft"로구분. Edison해석("지시서의draft강등은정책관문만의미")이 R-d·R-j와맞음. **단 설계서안에서 R-i("규칙수정삭제→draft강등")와§7.11 gate사유코드(rule_changed/rule_missing→cancel)가서로충돌** — 구현은cancel(안전방향). 설계서문구정리필요(L-1) |
| 5 | 수용 | 더보수적, 초안전용단계엔안전문제없음. 판정조건넓어사용성위험(L-5), R1-6실측뒤재검토 |
| 6 | **부분수용** | AR·Received원자료미룬것은Phase B에서타당(AuthGate없음). Phase C요구: auto_headers JSON v2로올리고 v1메일은"인증원자료없음→강등". engine.load_headers가v==1만받아함께수정. **is_backfill판정은결함(M-1)** |
| 9 | 기능상타당 | Phase B의paused_security는표시용(실제차단없음). 정상실패도security분류하면오경보반복. 설계서문구갱신필요, 보안판단은Spinoza |
| 11 | 대체로타당 | L-3참조 |
| 12 | **미해결→M-3** | 작성창경고배너없음 |

**목록밖 설계이탈1건(M-4)**: 계정자동회신탭의 신뢰AR ID·경계서버 입력란이 항상활성(account_dialog.py:389-394). §7.6③은 "ar_capability=confirmed아니면 자동탐지·수동입력모두비활성"이라규정.

## 4. 결함목록
| # | 심각도 | 위치 | 내용 | 권고 |
|---|---|---|---|---|
| M-1 | Medium(기능) | mail/sync_service.py:201 `is_backfill=not known_uids` | 로컬INBOX에remote_uid0건이면 다음받는메일전부backfill로봄. **재현**: INBOX전부보관함이동(받은편지함비우기)뒤새정상메일→excluded:backfill,잡0,로그0. 사용자가이유모름. 매뉴얼은"계정처음추가시"로만설명해실제와다름 | 폴더단위"첫동기화완료"표식을영속저장(folders.last_uid등), 로컬메일수와분리. 매뉴얼수정 |
| M-2 | Medium(출시게이트) | docs/research/01_R1_실측.md없음 | P3진입게이트미충족. argv(--tools "",--setting-sources,dontAsk),stream-json스키마,도구명접두사(TOOL_PREFIX)전부가정. 틀리면Claude작성잡전부claude_exit/security_tool(+paused_security)로끝남 | 출시전R1수행,Spinoza·데카르트승인 |
| M-3 | Medium | 작성창(§7.5마지막줄) | Claude초안에출력가드결과(URL·금액·계좌등)경고미표시. 인젝션피싱URL이초안에섞여도사람이놓칠수있음 | 출시전배너구현 |
| M-4 | Medium(PhaseC대비)/PhaseB영향없음 | account_dialog.py:389-394 | ar_capability계산(ar_presets)없이신뢰값수동입력가능. §7.6③,G0이탈 | PhaseC전까지입력비활성, 또는PhaseC서기존값"미확인"재판정 |
| L-1 | Low/문서 | DESIGN.md R-i↔§7.11 | 실행중규칙편집→잡cancelled,초안없음. 내용안바꾸고[저장]만눌러도version올라 진행중잡취소 | 설계서문구통일. 변경없는저장은version유지검토 |
| L-2 | Low(UX·매뉴얼불일치) | rule_editor_dialog.py:670 | "규칙에맞지않는메일"선택이 [닫기]버튼으로만저장, X나Esc(reject)로닫으면사라짐. 매뉴얼은"창닫을때저장" | reject/closeEvent서도저장또는즉시저장 |
| L-3 | Low | job_runner.py:231 | MCP서버꺼져있으면claude불필요한고정템플릿잡도queued로남음. 50건쌓이면새메일blocked(job_cap_queue) | 고정템플릿은MCP상태와무관히처리 |
| L-4 | Low(PhaseC서Medium) | IMAP UIDVALIDITY미추적(P1부터있던한계) | UIDVALIDITY바뀌면최근72시간메일재저장+재평가→초안중복(상한30건/시간) | PhaseC전UID상태추적 |
| L-5 | Low(사용성) | preflight.py:116-122 | MCP외allow규칙·플러그인하나라도있으면fallback3→Claude작성잡전부취소. 다용자대부분해당가능(이PC는깨끗함) | R1-6실측후setting_sources모드전환 |
| L-6 | Low | test_architecture_autoreply_phase_b.py:70-103 | G7함수안의 drafts_repo.mark_outbox(...) 호출이나변수로넘긴"outbox"전이는검출못함 | 금지호출명목록추가 |
| I-1~I-3 | Info | app.py, settings_dialog.py, 전역 | 낡은docstring, 타임아웃옛키저장, paused_security해제UI없음(PhaseB영향없음) | 정리. **I-3: G8보다먼저 판정값에자동발송추가하지말것(PhaseC착수조건)** |

## 5. 그외 검증결과
- **RE2**: 200자통과/201자거부(정규식함수+규칙저장검증양쪽). 역참조/전후방탐색/부정전방탐색전부거부. 대소문자구분안함. `(a+)+$`5만자도1초안끝남.
- **우선순위**: ignore→draft순서면ignore적용, ▲▼(set_priorities)로순서바꾸면draft규칙적용+rule_id도맞음.
- **드라이런**: claude미실행, DB6테이블해시변화없음(total_changes=0), auto_send규칙도"초안예정"표시. 단 드라이런은backfill·watermark·계정off같은제외조건미적용 — 실제와다를수있어안내문구추가권장.
- **LoopGuard**: 실제G1추출→엔진경로로1~11번17종투입, 전부blocked+잡0건+사유코드정확. 대조군(Auto-Submitted:no)은queued로정상평가. 17종: 빈Auto-Submitted, Precedence:Bulk, NoReply주소, bounces+주소, multipart/report, 한글전각제목, 내주소, X-EmailToMCP-Auto, From복수, Sender불일치 등.
- **UI**: 즉시발송항목비활성+툴팁(키보드Down/End로도선택안됨,offscreen확인). 토글off시 규칙목록·드라이런배너+잡탭[일시정지]/[재개]비활성(UI테스트확인). 저장된auto_send규칙을편집창에서열면draft로바뀌어보이고안내문표시.
- **매뉴얼대조**: 04·05·03·01·07이 UI문구·결과라벨·조건항목과대체로일치. 불일치는L-2(창닫을때저장), M-1(backfill설명) 둘뿐. manual_viewer SECTION_FILES에04등록됨.

## 6. R1 미수행 영향
코드수준판정엔영향없음(자동발송차단은R1무관하게구조보장, 고정템플릿경로는실제경로로완결). 출시게이트는보류("Claude작성"모드가실제claude에서동작하는지근거없음, 설계상진입게이트절차생략). 다음지시서에 R1실측(특히R1-3 stream스키마·도구명, R1-4/5/6, R1-14)을 Phase C착수전끝내고승인받기를넣을것.

## 다음단계 우선순위
1. M-1수정+매뉴얼정정 2. M-3배너구현 3. R1실측·승인 4. M-4: 입력비활성또는PhaseC재검증설계 5. L-1~L-3, 설계서문구(R-i, G6실패등급)갱신

## 관련 파일
`storage/repositories/autoreply.py`, `rules/{autosend_verify,engine}.py`, `mail/sync_service.py`, `autoreply/{job_runner,preflight}.py`, `ui/dialogs/{rule_editor_dialog,account_dialog}.py`, `docs/manual/04_규칙설정.md`, `DESIGN.md`(§7.0 R-i, §7.6③, §7.9 G6·G7)
