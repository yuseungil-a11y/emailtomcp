# MCP 연결안내 팝업 결함수정 — Edison

데카르트의 QA리뷰(38번) B-1(필수)·B-2(권장) 수정 + 여유로 B-3·B-4(Low)도 처리.

## 수정내용
- **B-1[High]**: `_build_claude_desktop_snippet()`을 `json.dumps(exe)`/`json.dumps(args)`로 안전이스케이프. 인자분리는 `rest.split()`대신 `shlex.split()`(파싱실패시공백분리폴백). 수동검증: `C:\Program Files\EmailToMCP\EmailToMCP.exe`, `C:\Users\UT\.venv\Scripts\python.exe -m emailtomcp`, `C:\new\tab\x.exe`(이전엔\n\t오인가능) 전부 json.loads() 정상파싱확인.
- **B-2[Medium]**: `_show_connect_info_popup()`에서 profile_name이"default"아니면 등록명령끝에 `--client <프로필>` 자동추가. 프로필이름은 `validate_proxy_client`(영문·숫자·_·- 1~32자)로제한돼 추가이스케이프없이안전. `--client`가등록명령문자열자체에포함되므로 `_build_claude_desktop_snippet()`이그대로파싱해 Desktop JSON args에도자동반영(별도로직불필요). 매뉴얼46-47행 "자동으로붙으므로따로덧붙일필요없음"으로갱신.
- **B-4[Low]**: `warning_label`에 `setTextFormat(Qt.TextFormat.PlainText)` 추가.
- **B-3[Low]**: `refresh_status()`후 등록명령비어있으면 빈ConnectInfoDialog대신 `plain_warning()`으로 "서버상태확인못해등록명령표시불가" 안내만표시.

## 회귀테스트(4건)
`test_claude_desktop_snippet_is_valid_json_for_windows_path`, `test_claude_desktop_snippet_is_valid_json_for_space_in_path`, `test_show_connect_info_popup_warns_when_status_unavailable`(B-3), `test_issue_proxy_token_with_named_profile_adds_client_flag`(B-2, 등록명령+Desktop JSON args 양쪽에 --client work 자동확인).

## 테스트결과
test_mcp_ui.py+test_dialogs_smoke.py: **33 passed**(기존29+신규4).

## 변경 파일
`ui/dialogs/mcp_panel.py`, `docs/manual/05_Claude_MCP패널.md`, `tests/ui/test_mcp_ui.py`
