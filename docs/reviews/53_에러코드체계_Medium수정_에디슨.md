# 에러코드체계 Medium결함 수정 — Edison

## Medium#1 — 발송/폴링실패로그에러코드누락
`mail/send_service.py`(157-181행): AuthError/PermanentError/TransientError 3개except블록의logger.warning에모두`extra={"error_code":exc.error_code}`추가. `update/checker.py`(281행): TransientError경로logger.info에동일추가. `app.py`(651행, _reflect_imap_move): except(TransientError,EmailToMcpError)블록logger.warning에동일추가. 세곳모두EmailToMcpError계열이error_code속성항상가짐(없으면None), _ErrorCodeFormatter가None안전무시확인.

## Medium#2 — 매뉴얼01_메인창.md오류3건
(a)"로그의에러코드"제목을"메뉴/툴바"글머리목록뒤로이동(툴바항목이딸려들어가는문제해결). (b)예시문구를describe()설명문대신실제로그형식으로교체(.venv로_ErrorCodeFormatter직접실행해확인: "초안5발송일시오류,재시도예정:SMTP통신실패:...[EMCP-2005]"). (c)"코드뒷자리"→"코드앞자리(천단위)"로수정+9xxx분류추가.

## Low(선택) — 둘다처리
`test_error_code_values_are_unique`를`ErrorCode.__members__.values()`(별칭포함)기반으로수정(기존iteration기반은별칭숨는중복못잡음). IMAP_OPERATION_FAILED/POP3_OPERATION_FAILED/UNKNOWN은실제부착지점신설안하고 "향후사용예정"주석만추가.

## 테스트결과
`test_error_codes.py`+`test_logging_setup.py`+`test_send_service.py`+`test_update_checker.py`(integration)+`test_imap_provider.py`(integration) → **61 passed**.

## 참고
47/48/50번(단일인스턴스)작업의미커밋변경이 app.py/local_handshake.py등같은파일에섞여있음 — Newton에게커밋순서(함께또는47/48/50먼저)확인필요.

## 변경 파일
`mail/send_service.py`, `update/checker.py`, `app.py`, `docs/manual/01_메인창.md`, `tests/unit/test_error_codes.py`, `core/error_codes.py`
