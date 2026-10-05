# P3 Phase B(규칙엔진+claude -p 잡실행, 초안전용) 보안검토 — Spinoza

> 참고: 전달과정에서 하네스가 "settings-json, bypass-permissions, permissions-allow-deny 패턴매칭" 경고를 띄웠음. 내용 확인결과 Claude Code의 설정격리(폴백3 판정: hooks/bypassPermissions/permissions.allow·deny 등)를 다루는 보안검토 본연의 내용이라 정상이며 실제 권한상승 시도 아님(제우스 판단). 참고로만 남김.

## 판정
- **코드수준(Phase B, 초안전용): 조건부 Go.** H-1은 배포 전 고쳐야함. M-1·M-2는 Phase B 안에서 고치길 권고.
- **출시게이트(Phase C, auto_send 열기): No-Go.** R1 실측보고서 없음, H-1·M-1·M-2 잔존, 판정기가 PreflightResult 사후점검을 강제하지 않음.
- 집계: Critical0, High1, Medium2, Low5. 테스트 3파일(test_mcp_job_tokens, test_autoreply_runner_parts, test_architecture_autoreply_phase_b) 35건 통과. DACL은 이PC에서 직접확인.

**자동발송 미개방 확인됨**: G7 INSERT는 'draft'고정, 판정값타입에 자동발송값 없음({draft,cancel,discard}뿐), 판정기통과해도 결과는 항상draft.

## High

### H-1. CLAUDE_CONFIG_DIR 때문에 점검설정과 claude가 실제로 읽는 설정이 달라질 수 있음(폴백3 판정 우회)
- `preflight.claude_config_dir()`는 앱프로세스의 `os.environ["CLAUDE_CONFIG_DIR"]`가 있으면 그 디렉터리를 점검. app.py가 JobRunner에 claude_config_dir=None, env_source=None 전달해 이 분기를 탐.
- `env_policy.build_child_env` allowlist엔 CLAUDE_CONFIG_DIR 없음 — 자식claude는 기본경로(`%USERPROFILE%\.claude\settings.json`) 읽음.
- 결과: 사용자환경에 이 변수 있으면 깨끗한디렉터리X를 점검·통과시키지만, 실제론 점검안한 `~/.claude`설정(hooks,plugins,permissions.allow:["Bash"]등)이 적용됨.
- 공격체인: R1-4(--tools ""), R1-6(--setting-sources project) 미실측이라 지금은 폴백3판정이 실제 방어선. dontAsk모드는 settings allow규칙을 사전승인으로 취급 — --tools ""가 기대대로 안되면 공격자메일의 프롬프트인젝션이 사용자설정 허용Bash로 이어질 수 있음.
- 수정안: 점검대상경로를 자식env에서 계산(같은Mapping으로 build_child_env와 claude_config_dir 함께결정). CLAUDE_CONFIG_DIR 설정돼있으면 (a)자식에게도 같은값 전달+그디렉터리 점검, 또는 (b)미지원구성으로 폴백3처리. 회귀테스트: env_source에 CLAUDE_CONFIG_DIR 넣었을때 점검경로=자식경로 단언.

## Medium

### M-1. 폴백3 판정이 denylist방식이라 빠진 위험항목 있음(`preflight.inspect_user_settings`)
보는항목: hooks, enabledPlugins, MCP외allow, defaultMode==bypassPermissions, apiKeyHelper, env접두사3종(ANTHROPIC_/CLAUDE_CODE_/NODE_)뿐. 빠진것:
- settings의 env블록이 자식env allowlist를 되돌림 — HTTPS_PROXY/HTTP_PROXY(메일내용 프록시유출, localhost예외없으면 잡토큰도노출), OTEL_*(텔레메트리로 프롬프트유출), CLAUDE_CONFIG_DIR, SSL_CERT_FILE 등 통과.
- 명령실행 설정키: statusLine(type=command), awsAuthRefresh, awsCredentialExport, otelHeadersHelper(apiKeyHelper와 같은부류).
- 시스템지침/모델 바꾸는키: outputStyle, model.
- managed settings(정책파일) 미점검 — Windows `C:\Program Files\ClaudeCode\managed-settings.json`(구버전 `C:\ProgramData\ClaudeCode\`), macOS `/Library/Application Support/ClaudeCode/`. --setting-sources로도 배제안됨.
- 사용자메모리 `~/.claude/CLAUDE.md` 미점검(조상점검에서 ~/.claude가 예외).

수정안: **allowlist방식으로 전환**(무해키만 허용, 그외 최상위키 하나라도 있으면 fallback3). env블록 비어있지않으면 fallback3. managed settings+~/.claude/CLAUDE.md를 점검·해시대상에 추가.

### M-2. 사후점검(TOCTOU완화)을 계산만하고 아무데도 강제안함
- `preflight.finalize`가 user_settings_unchanged, unexpected_files, ancestor_clean, jobdir_acl 계산하나, job_runner와 autosend_verify.py 어디서도 이값으로 분기안함(로그만).
- 사전점검(t0)~실행 사이 settings에 hooks추가(동시 Claude Code세션의 /hooks, 플러그인설치)해도 초안이 그대로 생성됨.
- Phase B가 "fallback3이면 초안도 안만듦" 택했으므로 동일기준 적용권고: user_settings_unchanged=False거나 unexpected_files 비어있지않으면 discard(security). ancestor_clean=False거나 jobdir_acl='failed'면 cancel(설계상 draft강등이 기준이나 정책일치를 위해 cancel권고). 판정기(G7④)에 추가, 해시대상도 M-1 확장파일 전체로.
- 실행중 바꿨다가 원복하는경우는 해시로 못잡음 — 동일사용자권한 필요한 위협이라 수용가능, 문서화권고.

## Low
- **L-1**: G6실패등급분리(허용외도구만security) — 피해측면 안전(permanent도discard=True). 단 탐지신호 소실: submit2회이상(G5서버가거부하나 인젝션전형신호가 조용히처리), turns초과도동일, 타임아웃경로는 stdout미파싱이라 허용외도구시도후멈춘경우 paused_security+경보빠짐. 수정: 타임아웃·overflow경로서도 parse_stream먼저 실행, submit_count>1은security분류, 나머지permanent는 suspicious태그로 Phase C 회로차단집계 포함.
- **L-2**: 계정신뢰필드저장시 드레인(Edison14번) — 위험동작없음(되돌리는방향, 바인딩파라미터, 현재대상0건). 다만 ①값변경여부 안보고 컬럼포함만되면 드레인(Phase C서 저장할때마다 대기outbox가 초안복귀, 가용성문제) ②in_host가 _TRUST_FIELDS에 없음(ar_capability가 수신호스트로 프리셋계산/§7.6③) — in_host/incoming_protocol/out_host/out_username 추가권고.
- **L-3**: CLI고정 — 다른바이너리 교체시 거부확인(verify_pin이 실행직전sha256대조). 확장자없는경로서 Windows가.exe로바꿔실행하는지 실측(WinError193거부, 우회없음). 남는문제: 해시대조~exec사이 TOCTOU(동일사용자쓰기권한필요), npm모드는 node+cli.js만해시 나머지모듈(vendor등)미고정 — 수용+문서화권고.
- **L-4**: 규칙삭제·계정비활성시 실행중프로세스 미정리 — DB만cancelled, abort_jobs(토큰폐기+kill) 미호출. J도구가DB status재확인+G7CAS있어안전하나 프로세스는 타임아웃까지계속돔. 커밋후 러너 abort_jobs호출권고.
- **L-5**: 판정기가 G6사실 재확인안함(tools_used⊆허용/submit_count==1/exit_code==0 검사가 러너에만있음) — Phase C전 판정기(G7)에도 넣어 이중방어권고.

## 요청항목별 확인결과
1. **env allowlist — 양호**: Windows SYSTEMROOT/WINDIR/USERPROFILE/APPDATA/LOCALAPPDATA/HOMEDRIVE/HOMEPATH+LANG/LC_*+TEMP/TMP(잡전용)+정제PATH+잡토큰만 전달. ANTHROPIC_*/CLAUDE_CODE_*/NODE_OPTIONS,NODE_PATH는 마지막에 재삭제. API키·클라우드·깃토큰·프록시 미전달. 남은문제는 H-1·M-1뿐. 참고(보안외): CLAUDE_CODE_GIT_BASH_PATH도삭제돼 Windows서 claude가git-bash못찾아 기동실패가능 — R1서 확인필요.
2. **잡디렉터리DACL — 양호(실측)**: icacls결과 루트는 `UT:(OI)(CI)(F)`하나(상속표시없음=보호DACL), work/와mcp-config.json은 `UT:(I)`상속만. SYSTEM/Administrators없음. mcp-config는 ${VAR}참조만(토큰값없음). ancestor_clean 로직은맞으나 결과강제안함(M-2).
3. **폴백3판정 — 불완전**(H-1,M-1,M-2).
4. **잡토큰분리 — 양호**: 메모리전용+mcp_tokens분리, ejob.접두사단일조회(폴백조회없음, '.'이 urlsafe알파벳에없어충돌불가), tools_for_principal이 kind먼저닫힌enum분기(그외값은빈집합), list_tools·call_tool양쪽검사+교차호출403테스트됨, submit성공직후토큰폐기+재사용401(테스트됨), 폐기실패해도 G5조건부UPDATE(submitted_at IS NULL)가재제출막음, 타임아웃·abort·finally경로서도폐기.
5. **실행인자·셸인젝션 — 양호**: argv는상수+앱생성경로뿐+shell=False, .cmd/.bat/.ps1/.vbs/.js고정불가. 메일본문·제목·발신자는argv·stdin에안들어가고 get_job_message도구로만전달. stdin엔사용자가정한규칙명·추가지침·계정명만, 제어문자제거후safe_substitute치환.
6. **G7해시대조 — 설계M-C충족**: 트랜잭션안jobs.submission_sha256 대 sha256(INSERT본문) 및 guard.body_sha256 대조, claude실행잡이면verified_body_sha256도대조. 판정결과타입+job_id/세대/rule_version바인딩검사, 예외·타입불일치면롤백+failed. 판정기가결과를억지로통과시킬구조적틈없음(보강은L-5).

추가확인: 비신뢰마커(mail/untrusted.wrap_untrusted 그대로재사용, From/To/Subject/첨부명/본문전부블록안, 마커위조이스케이프테스트됨). ReDoS(안전 — google-re2만사용 표준re폴백없음, 패턴200자+max_mem2MB+규칙당조건30개+본문10,000자제한). 참고: user settings의 MCP외allow는 일반사용자대부분이가져 사실상항상fallback3취소될수있음(가용성문제, UI에사유명확표시권고).

## 출시게이트(Phase C) 선행조건
1. R1실측: --setting-sources project, --tools "", dontAsk의 settings allow처리, stream-json스키마, ${VAR}확장, Windows git-bash의존성.
2. H-1,M-1,M-2수정. 판정기가 PreflightResult전항목(isolation,acl,ancestor,settings_unchanged,unexpected_files,G6사실) 강제.
3. L-1(탐지신호를 회로차단집계에 연결), L-2(드레인대상필드 보강).

## 관련 파일
`autoreply/preflight.py`(72-77,80-132,190-219행), `autoreply/env_policy.py`(56-125행), `autoreply/job_runner.py`(351-368,400-437행), `rules/autosend_verify.py`(88-110행), `storage/repositories/autoreply.py`(1176-1275행), `storage/repositories/accounts.py`(214-253행), `autoreply/{job_dir,cli_locator,claude_runner}.py`, `mcp_server/{job_tokens,tools_job}.py`, `mcp_server/server.py`(92-110,300-304행), `app.py`(1773행)
