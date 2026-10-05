# 잔여 Low 항목 6건 일괄정리 — Edison

## 요약
S-1, S-2, N-5, N-7, R-1, BUG-② 모두 수정 완료. 82 passed. R-2/R-3은 지시대로 생략(문서화만).

## 수정 내역

**S-1** `runtime/asgi_host.py`: `uvicorn.Server` 상속 `_NotifyingServer` 추가, `shutdown()` 오버라이드해 첫줄에서 `_notify_exit()` 호출 후 `super().shutdown()`. `_serve()`에서 이 서브클래스 사용. 회귀테스트: `test_mcp_http_binding.py::test_on_exit_runs_while_listening_socket_is_still_open`(콜백 안에서 같은 포트 접속시도로 소켓 열려있음 확인).

**S-2** `ui/message_box.py`: `make_plain_message_box`를 `QMessageBox(icon,title,"",...)`→`setTextFormat(PlainText)`→`setText(text)` 순서로 변경(다른 3곳과 통일). 회귀테스트: `test_dialogs_smoke.py::test_make_plain_message_box_sets_format_before_text`(setText 스파이로 호출시점 PlainText 확인).

**N-5** `ui/models/folder_tree_model.py`: `set_account_status`에서 `setToolTip(tooltip)`→`setToolTip(html.escape(tooltip))`. 회귀테스트 신설: `tests/ui/test_folder_tree_model.py`(`<img src=...>` 섞인 오류문자열이 `&lt;img`로 escape 확인).

**N-7** `mcp_server/approval.py`: 토큰단위 거부쿨다운(지수백오프) 추가. `TOKEN_REJECT_BACKOFF_BASE`(5분)/`TOKEN_REJECT_BACKOFF_MAX`(24시간), `_token_reject_count`/`_token_cooldown_until`. `reject()`가 토큰ID로 연속거부횟수 증가, 2회째부터 5분→10분→20분... 쿨다운. `_check_request_limits()`가 토큰쿨다운이면 새초안이어도 거부(기존 초안단위 60초쿨다운과 별개 검사). `approve()` 성공시 해당 토큰 횟수/쿨다운 초기화. 회귀테스트: `test_mcp_tokens_ratelimit.py::test_approval_token_reject_backoff_escalates_and_resets_on_approve`.

**R-1** `mail/mime/parse.py`: `_attachment_filename()`에서 Content-Type 8비트 name 헬퍼 호출 전, Content-Disposition이 ASCII(str)이고 filename 파라미터 있으면 그 값(RFC2231 collapse+`_decode_header_text`로 비표준 encoded-word도 디코딩) 우선반환 — `get_filename()`과 동일 우선순위 복원. 수정 중 회귀 발견: `nonstandard_encoded_word_filename.eml` 깨짐(ASCII분기 디코딩 누락) → `_decode_header_text` 추가로 해결. 새 fixture `raw8bit_cd_ascii_ct_8bit_filename_priority.eml`(CD=ascii.pdf, CT name=8비트 다른이름.pdf) + 테스트 `test_raw8bit_cd_ascii_filename_takes_priority_over_ct_8bit_name` 추가.

**BUG-②** `storage/repositories/messages.py`: `query_messages()`의 2자이하 LIKE 폴백에 ESCAPE 누락 — 기존 `escape_like()`/`_LIKE_ESCAPE_SQL`(search_message_meta가 쓰던 헬퍼) 재사용해 `LIKE ? ESCAPE '!'`로 통일. 회귀테스트 신설: `tests/unit/test_messages_grid_search.py`(`%`/`_` 한글자 검색시 리터럴 포함 메일만 일치 확인).

## 생략 항목 (지시대로, 문서화만)
- R-2(EUC-KR 짧은문자열 charset_normalizer 오판): charset.py 미수정, 알려진 제약사항으로만 기록.
- R-3(RFC2231 비표준 8비트): 니치케이스, 수정전부터 깨져있어 회귀아님, 기록만.

## 테스트 결과
`test_mcp_http_binding.py`, `test_mcp_runner.py`, `test_mime_parse.py`, `test_mime_build.py`, `test_mime_limits.py`, `test_mcp_tokens_ratelimit.py`, `test_messages_grid_search.py`(신설), `test_folder_tree_model.py`(신설), `test_dialogs_smoke.py`, `test_main_window_smoke.py` → **82 passed**.

## 참고 — 이번 작업과 무관하게 발견된 기존 실패 (⚠️ 트리아지 필요)
`test_mcp_tools.py` 전체(34건) 중 **2건이 이번 변경과 무관한 영역에서 이미 실패 중**이었음(approval.py/messages.py와 무관, 격리 재실행으로도 재현 확인):
- `test_get_message_reports_cc_attachment_mismatch_and_truncation`
- `test_get_thread_not_found_missing_thread_key_and_long_body_truncated`

원인 추정 영역: `mcp_server/tools_read.py`의 get_message/get_thread 본문 길이 절삭 로직, "...(이하 생략)" 문구. 이번 작업 범위 밖이라 미수정. **별도 트리아지 필요**(최근 여러 에이전트가 동시 작업한 영역이라 회귀 여부 확인 필요).

## 형상관리
수정 파일 대부분 기존부터 untracked 상태. 커밋 안 함(Newton 위임 대상).
