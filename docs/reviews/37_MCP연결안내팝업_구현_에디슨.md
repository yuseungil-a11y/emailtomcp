# MCP 연결안내 팝업 구현 — Edison

## 구현 내용
1. **토큰 발급 직후 연결안내 팝업**: `mcp_panel.py`에 `ConnectInfoDialog(QDialog)` 신설. stdio프록시 등록명령 전체(읽기전용QLineEdit), [복사], **[파일로 저장]**(QFileDialog, 명령+안내문 작성), [닫기]. Claude Desktop용 설정JSON 조각도 등록명령서 정규식파싱해 같이표시(`_build_claude_desktop_snippet`). 모든라벨 `_plain()`으로 PlainText고정. `McpPanelDialog._on_issue()`: `issued.plaintext`없을때(stdio프록시발급성공)만 이 팝업으로 교체. **HTTP직결토큰(plaintext있음)은 기존 TokenRevealDialog 흐름 그대로 유지**(1회표시+60초뒤클립보드자동삭제+파일저장버튼없음) — 건드리지않음.
2. **서버탭 재조회**: "stdio프록시 등록"명령줄 옆에 **[연결안내 다시보기]**버튼 추가, `_show_connect_info_popup(profile_name=None)`로 상태새로고침후 같은팝업 재오픈(발급직후·서버탭 둘다공유).
3. **매뉴얼동기화**: `05_Claude_MCP패널.md`의 "처음연결하기"·"서버탭" 절을 새팝업흐름(토큰발급→팝업서복사/저장/Claude Desktop JSON확인→재시작, [연결안내다시보기]안내)에맞게갱신.

## 테스트
`test_mcp_ui.py`에 회귀테스트4건: stdio프록시발급시팝업뜸, HTTP발급시기존TokenRevealDialog만뜨고새팝업안뜸(기존동작유지확인), [파일로저장]이실제파일작성, 팝업의모든QLabel이PlainText. 공용`_FakeApi.issue_mcp_token`이 kind=="proxy"일때 IssuedToken(plaintext=None)반환하도록보강(실제설계§6.4일치).

지정실행: test_mcp_ui.py+test_dialogs_smoke.py → **29 passed**(신규4건포함). ruff/mypy통과.

## 변경 파일
`ui/dialogs/mcp_panel.py`, `docs/manual/05_Claude_MCP패널.md`, `tests/ui/test_mcp_ui.py`
