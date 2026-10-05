# BUG-① 최종 검증 — 데카르트

## 판정: Go. BUG-①은 "종결" 처리 가능.
크래시(D-1)와 파일명 손상(D-2) 모두 해결. 남은 문제 3건(R-1~R-3)은 모두 크래시 아니고 심각도 낮아 종결을 막지 않음. 별도 이슈로 등록.

## 확인 결과
1. **D-1 크래시 수정 확인**: 줄번호 이동(예전 285→331, 296→342), 둘 다 `_unfold(...)`로 감싸짐 확인. 예전 코드 재실행 시 Content-Disposition/Content-ID 모두 AttributeError 재현. 수정후 parse_limited=False, 본문·첨부·content_id 정상.
2. **D-2 파일명 처리 확인**: `_filename_param_from_raw_header()`(parse.py:180-214)는 헤더값이 str이면 즉시 None 반환 — 순수ASCII는 기존 get_filename() 경로 그대로. 두 헤더 모두 8비트면 Content-Disposition filename 우선("CD이름.pdf" 확인). 단, 완전히 동일하진 않음(R-1 참조).
3. **신규 테스트 결정성**: 프로세스 내에서만 `_attachment_filename`을 예전로직으로 교체 실행(저장소 미변경) → 두 fixture 모두 깨진 파일명(`'������.pdf'` 등). 예전코드라면 D-1은 크래시, 파일명 assert도 반드시 실패 — 결정적 테스트 확인.
4. **지정 테스트 재현**: test_mime_parse.py+test_mime_build.py+test_mime_limits.py → 37 passed(0.21초).
5. **추가 헤더조합 점검**: parse.py 전체에서 `_unfold` 미경유 헤더처리 지점 없음 확인. 16개 조합(CD파라미터 복수+8비트혼합, 무따옴표8비트, 따옴표안세미콜론, 접힌헤더, RFC2231continuation+8비트 2종, CT charset/타입, CTE, CID 8비트, EUC-KR원시바이트) 직접 테스트 — 크래시·parse_limited강등 0건.
6. **기존 fixture 회귀 확인**: 이번추가 2종 제외 나머지 10종, 개별테스트8건+test_never_raises10건 모두 통과. 순수ASCII헤더는 새경로 미경유라 회귀위험 없음.

## 남은 문제 (크래시 아님, 종결 비차단)
- **R-1[하] 파일명 우선순위 역전**: parse.py:217-225. Content-Disposition filename이 ASCII(str)이고 Content-Type name이 8비트면 Content-Type쪽이 선택됨(재현: "ascii.pdf" 대신 "다른이름.pdf"). 기존 get_filename()이라면 Content-Disposition을 골랐을 것. 수정안: Content-Type 헬퍼 호출 전에 Content-Disposition이 str이고 filename 파라미터 있으면 그 값 우선사용.
- **R-2[하~중] EUC-KR 원시 8비트 첨부파일명이 잘못된 문자로 나옴**: "한글문서.pdf"→`'フ旋僥憮.pdf'`. 지난번 보고한 charset_normalizer 문자셋오판(charset.decode_bytes)과 동일원인이 첨부파일명 경로에도 나타남. 국내 레거시메일에 EUC-KR 원시파일명이 흔해 기존 오판이슈에 함께 묶어 등록 권고.
- **R-3[하] RFC2231 별표(filename*=utf-8'') 값에 퍼센트인코딩 없이 8비트가 들어간 비표준 입력**: 파일명이 이스케이프문자열(`'한글...'`)로 나옴(collapse_rfc2231_value 단계). 수정전 코드도 깨져있어 회귀 아님.
- **참고[하, 범위밖]**: multipart boundary 자체가 8비트인 비표준메일은 본문·첨부가 비는데 parse_limited 표시도 안 붙음(조용한 소실). RFC위반 입력이라 발생빈도 극히낮음.
- **O-1(지난번 지적) 그대로**: mail/, 테스트, fixture 미커밋 — Newton 위임 필요.

## 관련 파일
`src/emailtomcp/mail/mime/parse.py`, `tests/unit/test_mime_parse.py`, `tests/fixtures/eml/generate_fixtures.py`
