# BUG-① 재검증 — 데카르트

## 판정: 조건부 Go
From/Subject 등 주소·식별 헤더에서 크래시가 나던 원래 증상은 고쳐졌고 회귀도 없습니다. 다만 **같은 원인의 크래시가 첨부 파트 헤더 2곳에 그대로 남아있어(D-1), 이를 고치기 전에는 BUG-① "종결"로 처리하면 안 됩니다.**

## 확인한 것
1. **코드 확인(parse.py:82-117)**: `_unfold`는 값이 str이 아니면(Header 객체) `_decode_header_text`로 먼저 변환 후 `or ""`를 붙여 항상 str 반환. `_decode_header_text`도 예외경로 포함 항상 str 또는 None 반환. 문제없음.
2. **호출경로 grep**: `_parse_message`(254-266), `_limited_result`(362-365) 확인. From/To/Cc/Bcc/Reply-To/Message-ID/In-Reply-To/References/Date/X-Priority/Importance 전부 `_unfold` 경유. Edison 보고서에 빠진 Sender(259)도 `_parse_address_list` 경유로 보호됨(직접 검증: Sender/To/Cc/Reply-To 8비트 한글 정상추출). 다만 "전부 점검"은 부정확 — D-1 참조.
3. **신규 테스트 결정성**: 프로세스 내에서만 `_unfold`를 구버전으로 되돌려 실행(저장소 git 작업 없이) → fixture 2종 모두 parse_limited=True, from_addrs=[], message_id=None. 수정 전 코드라면 확실히 실패하는 결정적 테스트임을 확인.
4. **지정 테스트 재현**: test_mime_parse.py+test_mime_build.py+test_mime_limits.py → **33 passed(0.21초)**.
5. **범위 밖 2건, 이번 판정 비차단**: BUG-②(messages.py:399-401) 그대로 남음, 결과 부정확 정도로 인젝션 위험 없어 정기패치 대상. charset_normalizer 오판(EUC-KR "김" 한글자→"梯", "홍길동" 3글자부턴 정상) 직접 재현 — 크래시 아닌 표시품질 문제, 국내메일 흔한 1~2자 이름이라 별도이슈(심각도 하~중) 등록 권고.
6. **회귀 없음**: 기존 fixture 8종 전부 통과. RFC2047 인코딩 UTF-8/EUC-KR 헤더, ASCII 헤더 직접 검증 정상.

## 발견한 결함

### D-1 [심각도: 상, 수정 필요 — BUG-① 잔여분]
첨부 파트 헤더에 인코딩 안 된 8비트 바이트가 있으면 여전히 크래시 → 메일 전체 parse_limited=True로 떨어져 본문·첨부 전부 소실.
- 위치1: parse.py:285 `(part.get("Content-Disposition") or "").lower()` — Header 객체엔 `.lower()` 없어 AttributeError.
- 위치2: parse.py:296 `content_id.strip()` — Content-ID가 Header 객체면 동일하게 실패.
- 재현: multipart 첨부 파트에 `Content-Disposition: attachment; filename="한글문서.pdf"`처럼 UTF-8 바이트를 그대로 넣으면 limited=True, body='', att=[]. Content-ID도 동일 재현.
- 국내 레거시 메일클라이언트가 인코딩 안 한 한글 첨부파일명을 실제로 흔히 보냄. Edison의 "RFC상 ASCII 토큰이라 범위밖" 판단은 실제 수신 메일 기준으로는 맞지 않음.
- 수정안: 두 줄 모두 `_unfold(...)`로 감싸면 됨. "8비트 첨부파일명" fixture 1종 추가 필요.

### D-2 [심각도: 중, 별도 이슈 권고]
`Content-Type: ...; name="<8비트 한글>"`처럼 Content-Type 쪽에 파일명이 있으면 크래시는 없으나 `get_filename()`(parse.py:180-188, `_attachment_filename`)이 깨진 문자열(`'������.pdf'`) 반환 — 원래 바이트를 재디코딩하는 경로 없음.

### O-1 [형상관리, 참고]
`src/emailtomcp/mail/` 전체와 `tests/unit/test_mime_*.py`, `tests/fixtures/`가 git에 한 번도 커밋 안 됨. 커밋 시 Newton 위임 필요.

## 관련 파일
`src/emailtomcp/mail/mime/parse.py`, `tests/unit/test_mime_parse.py`, `tests/fixtures/eml/generate_fixtures.py`, `docs/reviews/16_버그수정_BUG1_에디슨.md`
