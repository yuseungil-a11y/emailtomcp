# P3 Phase B 보완 재확인 — Spinoza

> 참고: 전달과정서 하네스가 "settings-json, bypass-permissions, permissions-allow-deny 패턴매칭"경고. 내용은 Claude Code 설정격리(allowlist/폴백3) 보안검토 본연내용으로 정상, 실제권한상승시도아님(제우스판단).

## 판정
- **코드수준(초안전용): Go.** H-1,M-1,M-2 실제해결 확인(직접재현). 신규 Critical·High·Medium 없음.
- **출시게이트(Phase C auto_send개방): 여전히 No-Go.** 이번보완이 H-1·M-1·M-2는 제거했으나, 남은선행조건 4가지: R1실측보고서없음, L-5미반영(판정기가G6사실 재확인안함), L-2미반영(_TRUST_FIELDS에in_host등빠짐+값변경여부미확인), 출력가드금액탐지누락(Phase C서ok=False강등시 자동발송우회경로됨).
- 집계: Critical0,High0,Medium0,Low6(신규5+기존미해결2건재분류),Info3.
- 테스트: 관련파일만, test_autoreply_runner_parts+test_architecture_autoreply_phase_b+test_mcp_job_tokens 합계42건+test_autoreply_pipeline 54건 전부통과.

## 중점검증

**1. H-1해결확인(재현함)**: job_runner.py(393-428행)가 env_source하나를 run_pre_checks와build_child_env양쪽에전달. preflight.claude_config_dir()가 env_policy.child_home_dir()사용해 자식전달키(Windows=USERPROFILE/그외=HOME)와조회규칙동일. app.py(1811행)는claude_config_dir·env_source안넘겨기본값(os.environ)동작. 재현결과: CLAUDE_CONFIG_DIR=깨끗한디렉터리+홈~/.claude도깨끗한경우도 fallback3(사유unsupported_env:CLAUDE_CONFIG_DIR) — 원래우회시나리오막힘. 소문자claude_config_dir도Windows서탐지. 모든경우자식env에CLAUDE_CONFIG_DIR안들어감+claude_config_dir(원본)==claude_config_dir(자식env). 빈문자열값은미탐지하나자식에도안넘겨무해.

**2. M-1 allowlist완전성(원래지적전부커버확인)**: env블록(비어있지않으면무조건걸림→OTEL_*·SSL_CERT_FILE·CLAUDE_CONFIG_DIR도함께막힘), statusLine/awsAuthRefresh/awsCredentialExport/otelHeadersHelper/apiKeyHelper, outputStyle/model/forceLoginMethod/enableAllProjectMcpServers, permissions(allow에Bash나리스트아닌경우/additionalDirectories/defaultMode acceptEdits·bypassPermissions), enabledPlugins(dict·list모두)/hooks — 전부fallback3확인. managed-settings.json도같은allowlist, managed-mcp.json은파일있기만해도fallback3. ~/.claude/CLAUDE.md해시대상포함+내용변경시해시변경확인. fail-closed: JSON파싱실패/읽기불가→unreadable로fallback3, 대소문자만다른키(Hooks)도걸림. permissions.allow의mcp__시작규칙은허용(argv의--strict-mcp-config로다른MCP서버로드안돼수용가능).

**3. M-2사후점검강제확인**: autosend_verify.py(111-124행)가claude실행잡에 4값모두분기적용(user_settings_unchanged아니거나unexpected_files있으면discard, ancestor_clean아니거나jobdir_acl≠owner_only면cancel). discard는autoreply.py(1246-1257행)서error_class=security+pause_security=True처리. 사후해시는pre.config_dir·pre.managed_dirs 그대로재계산 — 사전사후점검대상동일. TOCTOU창: 사전점검은토큰발급·spawn직전(밀리초), 사후점검은프로세스종료직후·G7전 — 실행중지속변경은전부탐지. 남은창은"실행중바꿨다가원복"뿐(동일사용자권한필요,수용가능,문서화됨). 판정기남은틈은L-5(G6사실재확인)하나.

**4. 출력가드정규식수정(범위조정2번) — 보안상안전**: 금액정규식끝경계조건제거라탐지범위확대방향 — 오탐늘수있어도우회신규생성없음. 영문단위쪽은(?![a-z])붙여"USDT"같은오탐막음. 성능실측: 패턴이단순수량자하나뿐이라지수형ReDoS구조없음. 숫자만4000자입력서 새금액정규식0.21초/옛형태0.20초(변경으로안늘어남). guard.check전체최악약1.5초(가장느린건기존URL도메인정규식0.9초). 입력은submit의body_text max_length=4000제한+러너스레드라수용가능.

**5. 잡탭[초안열기](범위조정1번) — 새공격면없음**: mcp_panel.py(634-753행)는로컬Qt UI전용. 잡행의result_draft_id로기존UiApi get_draft호출, status='draft'인경우만ComposeWindow.open_existing. MCP도구나외부경로로미노출. 로컬사용자가원래접근권한가진같은DB초안여는것이라소유권우회아님. 발송도사람이작성창서직접(초안전용설계일치). 경고배너·표의제목·보낸사람은PlainText.

**6. L-4 revalidate_current — 레이스없음**: abort_jobs는토큰폐기루프다마친뒤kill(순차). revalidate는DB cancel먼저커밋후토큰폐기 — 폐기직전이미처리중이던MCP요청도G5의DB상태확인서막힘. spawn경합: spawn전은_aborted확인서걸려미실행, spawn직후는handle저장후재확인해kill(테스트로spawn직후경로단언확인). 잡바뀌는경우 _current_job in ids조건으로엉뚱한프로세스kill안함. G7진입후면abort무효하나G7gate가cancel해안전. L-1반영(submit_count>1 security분류, 타임아웃경로stdout파싱)도확인.

## 신규·잔존 Low
- **N-1(신규)**: 홈키없을때경로어긋날수있음 — source에USERPROFILE없으면preflight는Path.home()(HOMEDRIVE+HOMEPATH)쓰나 자식Node는USERPROFILE없으면OS프로필API로홈찾아 둘이다를수있음(동일사용자가환경조작해야하는상황). 수정: child_home_dir()가None이면 unsupported_env:home_missing으로fallback3.
- **N-2(신규)**: 사용자수준다른구성요소미점검 — ~/.claude.json과~/.claude/{agents,skills,commands,plugins,output-styles,rules} 미점검(현재--strict-mcp-config,--tools "",enabledPlugins검사로완화되나 --tools ""동작미실측). 수정: R1실측항목에"user수준skills·agents·rules로드여부"추가.
- **N-3(신규)**: abort와보안분류순서 — _run_claude서aborted확인이_security_failure보다먼저라, 사용자가같은시점에잡중지하면허용외도구시도신호(paused_security)소실. 수정: aborted인경우도parse_stream으로보안판정먼저후aborted반환.
- **N-4(기존한계·Phase C관련)**: 출력가드금액탐지누락 — "100만원"(공백),"100만","일백만원","백만원" 미탐지. **Phase B엔경고배너만이라괜찮으나 Phase C엔 ok=False시강등이라 이누락이곧자동발송우회경로됨.** 수정: `\d[\d,.]*\s?(?:만|억|천)?\s?(?:원|달러)`처럼단위사이공백허용+단독"만/억"포함, 한글수사(일~구,십·백·천·만·억)+원패턴추가.
- **N-5(신규·가용성)**: 다른Claude Code세션실행중user settings수정시 매번보안discard+paused_security(Edison언급 allowlist전환으로인한fallback3증가와같은성격). UI사유표시+R1실측으로빈도확인권고.
- **L-2·L-5(지난보고서,이번미반영)**: Phase C게이트선행조건유지.

## Info
- Info-1: 출력가드다항(최대이차)시간정규식여럿(URL도메인,이메일난독화,금액). 현재4000자상한으로최악약1.5초. 고정템플릿경로도같은가드타므로템플릿길이상한확인권고, 상한올릴일생기면re2전환또는가드입력절단.
- Info-2: os.environ을사전점검·build_child_env서각각따로읽음 — env_source=dict(os.environ)으로한번스냅샷뜨면같은Mapping엄밀보장.
- Info-3: JobRunner의claude_config_dir덮어쓰기인자(테스트용)잔존 — 운영코드서쓰면H-1재발이라 app.py가이인자안넘긴다는아키텍처테스트추가권고.

## Phase C 출시게이트 선행조건(갱신)
1. R1실측: --setting-sources project/--tools ""/dontAsk의settings allow처리, stream-json스키마, ${VAR}확장, Windows git-bash의존성, work/파일생성여부, N-2(user수준skills·agents로드).
2. L-5: 판정기서tools_used⊆허용/submit_count==1/exit_code==0재확인.
3. L-2: 드레인대상필드보강(in_host,incoming_protocol,out_host,out_username)+값변경여부비교.
4. N-4: 금액탐지보강.
5. N-3반영+DESIGN의G6실패등급문구갱신.

## 관련 파일
`autoreply/{preflight,env_policy}.py`, `autoreply/job_runner.py`(187-242,393-521행), `rules/autosend_verify.py`(111-124행), `rules/output_guard.py`(58-65행), `ui/dialogs/mcp_panel.py`(634-753행), `storage/repositories/autoreply.py`(1246-1257행), `storage/repositories/accounts.py`(214-253행,L-2), `app.py`(418-422,1044-1079,1811-1821행)
