# P3 Phase B 3차 재검증 결함 수정 — Edison

## 요약
M-5, L-7, N-4/L-8 세 건 모두 수정. 지정테스트 100건(test_sync_service10+test_migrations35+test_autoreply_runner_parts55)+아키텍처테스트13건 전부통과. ruff/mypy(변경파일) 통과.

## 1. M-5 — 실패메일1통때문에 완료표식안찍히던문제
`mail/sync_service.py _poll_folder`: 최초동기화목록을 끝까지훑으면 일부실패해도 `initial_sync_done`즉시찍음("목록전체저장성공"조건제거). 실패UID는 `_pending_backfill_uids`(폴더별,메모리,프로그램실행중만유지)에기억 — 같은세션재수신시backfill처리, 저장성공시목록서제거. 한계: 재시작후받으면일반평가대상(72시간제외규칙+초안만생성이라영향작음, 주석명시). 중간에끊긴경우(목록조회·연결오류)는예전처럼표식안찍고다음동기화가다시backfill. 매뉴얼04:104-107 실제동작에맞게수정. 신규테스트: 데카르트재현시나리오(uid"bad"항상예외, 첫동기화직후표식찍힘+2·3번째새메일is_backfill=False)+기존테스트에"즉시찍힘"확인추가.

## 2. L-7 — 업그레이드전 받은편지함비운사용자
`m0004_folder_initial_sync.py`: 폴더단위판정에 INBOX전용판정추가 — 같은계정어느폴더든remote_uid있는메일있거나 original_folder_id가해당INBOX인메일있으면1. 신규테스트2건(휴지통remote_uid있을때 INBOX=1·Trash=1·Empty=0, remote_uid없이original_folder_id만INBOX일때 INBOX=1·Local=0).

## 3. N-4/L-8 — 출력가드금액탐지보강
`rules/output_guard.py _MONEY_RES`:
- 숫자형태제한: `(?<![\d,.])\d(?:[\d,]*\d)?(?:\.\d+)?`로 '.'·공백안삼킴 — "1.원인"번호목록더이상안잡힘, 숫자열중간매칭도없음.
- '원'앞숫자길이구분: 3자리이상·쉼표있으면항상금액("1500원인데"잡힘). 1~2자리+원은뒤에'원'시작낱말오면제외(인/본/문/활/칙/소/격/하/화등33자목록).
- 단위사이공백허용("100만 원","1백만원","2억 원","3천만 달러").
- "만/억"단독표기탐지("100만"잡힘, 뒤에년·명·제·측오면제외→"2억년","3억제","100만 명"안잡힘).
- 한글수사+원/달러탐지: "일백만원","백만원","오십만 원","금일백만원정","가격이백만원" 잡힘. 십·백·천·만·억중하나필수(→"사원","구원","일원","조원"제외). 낱말중간시작안함(→"불만 원인"제외). 30자길이제한으로백트래킹비용제한.
- 성능: 4000자공격입력서금액정규식각1ms미만. "1."*2000입력의check()전체는약1.2초(대부분기존URL등다른정규식, Spinoza Info-1과동일성격).

신규테스트31건: 오탐16건전부안잡힘확인(데카르트사례12+추가4: 불만원인분석/사원3명/100만명/조원들에게공유), 탐지15건전부잡힘확인(Spinoza미탐사례8+기존탐지유지7).

## 남은한계(Phase C전참고)
- 미탐: 1~2자리+'원인데'("50원인데")는이제안잡힘(오탐제외규칙대가, 금액작아수용). "100USDT"·띄어쓰기없는한글수사도아직미탐(USDT는데카르트가Phase C전별도검토권고항목).
- **보안재확인권고**: 이정규식은판정로직이라 Phase C게이트전 Spinoza재확인권고(Edison 자체권고).

## 변경 파일
`mail/sync_service.py`, `storage/migrations/m0004_folder_initial_sync.py`, `rules/output_guard.py`, `docs/manual/04_규칙설정.md`, `tests/unit/{test_sync_service,test_migrations,test_autoreply_runner_parts}.py`

---

# P3(Phase A+B) 사이클 종료 요약

이번 사이클(docs/reviews/23~35번, 13개 문서)로 P3의 Phase A(전역토글)와 Phase B(규칙엔진+claude -p 잡실행, 초안전용)가 **코드수준 Go** 상태에 도달했습니다.

- 자동발송(auto_send)은 7겹 이상의 방어로 완전히 차단된 채 유지되며, 공격 시나리오 재현으로 직접 확인됨.
- 보안검토 2라운드(설계단계+구현단계)에서 나온 모든 High/Medium 항목이 해소됨.
- 남은 항목은 전부 Low/Info이거나, **Phase C(실제 자동발송 개방) 출시게이트의 선행조건**으로 분류됨.

**Phase C 착수 전 반드시 필요한 것(R1 실측)**: 실제 Claude Code CLI를 설치한 환경에서 `--tools ""`, `--setting-sources`, `dontAsk` 모드의 실제 동작, stream-json 스키마, `${VAR}` 확장, Windows git-bash 의존성을 확인해야 함. 이 세션 환경엔 CLI가 없어 수행 불가 — 사용자 실제 환경에서 필요.
