# P3 Phase A(자동회신 전역 토글 인프라) 기능검증 — 데카르트

## 판정: 조건부 Go
Phase A 범위 기능은 설계와 일치. Critical/High 없음. Low 3건+문서정합성 2건은 Phase B에서 G7을 붙이기 전까지 정리 필요.

## 1. 테스트 재현
- 신규 5파일+test_migrations.py(m0003 8건 포함): **118 passed**(6.6초). Edison 보고 "128 passed"와 10건 차이 — 실패는 없고 숫자만 다름.
- P2 회귀(test_mcp_tools/test_draft_service/test_send_service/test_transitions/test_settings): **61 passed**, 보고와 일치.
- 직접 작성한 검증테스트(scratchpad, 저장소 미수정): **52 passed**. 보호키6×변형8종(대문자/Title/앞뒤공백/탭개행/CRLF/부분대문자)=48건 전부 PolicyError+settings테이블 불변. 보호키 아닌 키(autoreply.unmatched_action)는 정상쓰기 가능. settings.key는 대소문자구분 PK라 정규화비교 외 우회경로 없음.

## 2. 항목별 검증

### 2-1. 세대 초기값·m0003 재설정 로직
**초기값1 삽입은 타당** — 설계서 3곳(DESIGN.md:1207,:1211,:1529) 모두 "m0003에서 1로 시작" 명시. 지시서의 "0→1"이 설계서와 어긋난 것이고 Edison이 설계서를 따름. 첫켜기 1→2, m0003이후 ON세대는 항상 2이상 — L-B("ON세대≥1") 충족.

**무효값 1덮어쓰기(m0003:84-91)는 L-B 실질위반 아님(문구상만 어긋남)**:
- user_version 2→3 전이시 **딱 한번만** 실행(런타임 반복경로 아님).
- 유효값(bool아닌 1이상 정수)은 보존 — 유효세대 감소 없음(0/음수→1은 오히려 증가).
- 같은트랜잭션에서 enable_gen컬럼 방금추가(전부0)+autoreply.enabled삭제(OFF화) — ABA(옛세대가 새세대와 일치) 발생불가.
- 결론: "세대재사용"이 아니라 m0003의 "초기화"에 해당. 단 DESIGN.md:1211 문구와는 글자그대로 충돌 → **설계서를 "m0003 1회초기화: 유효값(≥1)보존, 무효값만1로설정"으로 수정 권고(코드는 안고쳐도됨)**.
- 예외(Info): enable_gen컬럼을 수동으로 미리추가한 개발DB는 0아닌 잡이 남을수있으나(`_add_column_if_missing`이 건너뜀), Phase A엔 잡생성코드가 없어 실위험없음.

**진짜 "재설정 경로"는 마이그레이션이 아니라 저장소쪽(Low, 직접재현)**:
- **D1**: `autoreply.py:137-139,:364-374` — `_read_generation`이 키없으면 0반환, `enable`이 0에서 켜기허용. 세대3까지올린뒤 settings의 generation행 DELETE→`enable(expected_generation=0)`하면 세대가 **1**이 됨(재현). 반복하면 같은세대번호 재출현 가능. 앱 안엔 이 키를 지우는 코드없고 set_setting도 막혀있어 DB파일 직접조작시에만 발생(Low). 권고: enable에서 키없음을 "손상"으로보고 거부(gate규칙의 "키없으면 disabled, fail-closed"와 일관).

### 2-2. drain_autosend_outbox 완성(스텁아님) — **타당, 범위 안**
§7.11(DESIGN.md:1511)이 시그니처 확정, 끄기1c(:1222)가 이 함수사용 명시, §12.3(:2005)의 "끄기드레인: outbox→draft"는 스텁으론 검증불가. 본체는 transition()만 호출+미커밋(호출자트랜잭션 참여), 대상은 `status='outbox' AND approved_by='policy_auto_send'`로 한정(sending/send_unknown/failed 안건드림). Phase B/C로미룬부분(M-A경로호출, send_unknown보강로그)도 주석에 정확히 명시됨.

### 2-3. 나머지 차이항목(3~7번) — 전부 설계와 일치하거나 타당
get_toggle_state(conn)/UiApi분리(§7.11 지시와 일치), 사전점검숫자 실제집계(문제없음), 이미켜진상태 enable거부(watermark보호, 타당), 꺼진상태 disable도 세대+1(단조성유지), 상태줄표시(§12.3 UI기준과 일치).

### 2-4. I8 불변식 — transitions.py:37-41,:74-78
to_state='draft'+테이블drafts일때만 CASE식3개 적용, approved_by='policy_auto_send'행만 초기화 나머지는 보존(SQLite SET식은 갱신전값 기준). user_send/ui/policy_allowlist 보존 테스트통과(test_drafts_i8_reset.py:106-119). outbox→sending 등 draft아닌전이는 승인정보유지(:92-100). `commit:bool=True`가 기존동작 안바꿈 — drafts.py의 transition()호출 7곳 전부 commit키워드 미사용(기본값True로 기존과 동일), commit=False는 autoreply.py에서만 사용. drafts.status를 transition() 거치지않는 직접UPDATE 없음(grep확인) — draft로가는 모든경로에 초기화규칙 자동적용.

### 2-5. L-A(끄기CAS없이항상성공) — autoreply.py:311-347, **확인**
expected_generation 인자/비교로직 없음(CAS없음 확인). 정상세대면 항상+1(재현: 세대7→끄기→8→끄기→9). 세대손상("x")시도 끄기성공, enabled=false만씀, 세대는None유지, 이후켜기는PolicyError로거부(재현확인). controller.py:151-172도 CAS없음, 메모리_active를 커밋전 먼저내림(안전방향), DB예외나도OFF유지+예외전파.

### 2-6. UI확인창 — settings_dialog.py:107-181
기본버튼[취소]setDefault(True), [켜기]setAutoDefault(False)(:137-142). 1.5초잠금: 양버튼 비활성시작(:149-150), showEvent에서 단발타이머(INPUT_LOCK_MS=1500, send_approval_dialog.py:44 재사용)(:160-164), 잠금중keyPressEvent무시(:172-176)+_on_enable_clicked에서 재차차단, 잠금해제시 포커스[취소]로(:170). 취소/CAS실패시 창안닫힘+상태재조회(:451-459). 끄기는 확인창없이 즉시+평문결과창(:425-442). UI테스트12건 통과.

### 2-7. G8없는상태에서 I1 위험여부 — **현재 실위험없음, Edison 판단 동의**
approved_by 쓰는곳 3곳뿐, 전부 코드고정값(app.py:807 "user_send", mcp_server/approval.py:211 POLICY_ALLOWLIST, :250 UI). mark_outbox에 외부입력경로 없음. 'policy_auto_send'는 transitions.py(초기화)+autoreply.py(조회·드레인)에서만 읽고 **쓰는코드는 0곳**. m0001 CHECK제약은 허용하지만 DB직접조작 없이는 이값가진행 생길수없음. MCP도 설정/approved_by 쓰는도구없음.
- **주의**: test_drafts_i8_reset.py:92-100처럼 이값가진 outbox행이 생기면 현재 mark_sending은 검사없이 통과시킴 — **Phase B/C에서 G7(행생성)이 G8(발송직전검사)보다 먼저들어가면 안됨.** Phase B 지시서에 이 순서조건 명시 필요(Spinoza의 M-1과 동일 결론, 독립확인).

## 3. 결함·지적 목록
| # | 심각도 | 위치 | 내용 | 권고 |
|---|---|---|---|---|
| D1 | Low | autoreply.py:137-139,:364-374 | 키없으면 세대0으로보고 켜기허용 — 키삭제후켜면 세대1 재출현(재현됨). 런타임의 유일한 세대재설정경로 | enable에서 키없음을 손상으로보고 PolicyError |
| D2 | Low | 아키텍처테스트 누락 | §7.11(:1490,:1531)의 "'policy_auto_send'는 허용파일에만" 테스트없음(현재 transitions.py+autoreply.py 두곳 참조) | Phase B에서 G7 넣기전 추가(허용목록: core/models.py, m0001, transitions.py, repositories/autoreply.py) |
| D3 | Low | app.py:1526-1535 | 기동복구 실패/15초타임아웃나도 Outbox루프 그대로시작(현재 대상행0건이라 무위험) | G8구현때 반드시 함께해소(Phase B완료조건 포함) |
| D4 | 문서 | DESIGN.md:1211 | "재설정경로없음(마이그레이션포함)"이 m0003의 1회무효값초기화와 글자그대로충돌 | "m0003 1회초기화(유효값보존)"로 수정 |
| D5 | 문서 | autoreply.py:357 docstring | "(최초0→1)"표기이나 실제론 m0003이후 첫켜기가 1→2 | 주석수정 |
| D6 | Info | controller.py:164-166 | 끄기중DB예외시 이벤트미발행, 상태줄이 "●켜짐"으로 잠깐남을수있음(실제차단은DB값기준이라안전) | 예외경로에서도 _publish() 호출검토 |
| D7 | Info | 구현보고 | 테스트건수 128로보고했으나 동일파일기준 118건 | 보고정정 |

Phase B 이월(타당, 추적대상): G3~G8, check_autoreply_gate, send_unknown→draft 보강로그, auto_reply_log.outcome CHECK에 autosend_reset_unknown 추가(rebuild_table), 설정저장(M-A)경로 드레인호출, 끄기2단계 순서단언테스트.

## 4. Go 조건
Phase A 커밋 자체는 **Go**. Phase B 착수지시서에 반드시 포함할 3가지:
1. G7보다 G8을 먼저/동시 구현(D3)
2. 'policy_auto_send' 아키텍처테스트 추가(D2)
3. D1 수정
D4·D5는 다음 설계서 갱신때 함께 반영.

## 관련 파일
`storage/migrations/m0003_autoreply_toggle.py`, `storage/repositories/autoreply.py`, `storage/transitions.py`, `autoreply/controller.py`, `app.py`(130-138,853-921,1521-1535), `ui/dialogs/settings_dialog.py`(107-181,411-459), `DESIGN.md`(1207,1211,1490,1511,1531,2002-2017)
