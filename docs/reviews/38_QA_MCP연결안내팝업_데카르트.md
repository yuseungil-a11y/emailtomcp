# MCP 연결안내 팝업 검증 — 데카르트

## 판정: 조건부 No-Go
보안쪽(토큰평문경로)은문제없음. 다만주사용환경인Windows에서 Claude Desktop용JSON이깨진채표시+저장됨(B-1). 수정후Go전환가능.

## 항목별결과
1. **HTTP토큰흐름유지 — 통과**: `_on_issue()`(mcp_panel.py:658-661)는 `if issued.plaintext:`로 TokenRevealDialog/_show_connect_info_popup분기, 섞이는경로없음. TokenRevealDialog(277-315) 불변(1회표시·마스킹·60초후삭제·파일저장버튼없음). 백엔드auth.py:172도proxy발급시plaintext=None, 평문은keyring에만저장 — 새팝업에토큰평문유입불가.
2. **[파일로저장]내용 — 통과**: `_file_content()`는고정문구+등록명령(app.py:334 stdio_registration_command())+JSON조각뿐. 등록명령엔토큰·비밀값없음. 저장파일엔실행파일경로(사용자명포함)만남음(허용범위).
3. **`_build_claude_desktop_snippet` — 결함(B-1)**.
4. **QLabel PlainText전수확인 — 부분통과**: 신규ConnectInfoDialog는전부`_plain()`사용. 단같은파일기존QLabel 13곳(227,237,244,285,477,480,484,491,505,515,519,524,698행)은 PlainText미지정(대부분고정문구라위험낮음, 단`warning_label`(484)는status.get("error")예외문자열들어가 PlainText고정권고 — 이번변경무관,기존부채).
5. **지정테스트 — 통과**: test_mcp_ui.py+test_dialogs_smoke.py 29 passed(Edison보고와일치). 단신규테스트엔JSON유효성검증없어B-1못잡음.
6. **매뉴얼대조 — 대체로일치**: 발급직후팝업/[복사]/[파일로저장]/[연결안내다시보기]흐름은실제UI와맞음. 단45행"JSON조각을그대로추가하면연결"은 B-1때문에Windows서사실아님.

## 발견결함

**B-1[High,기능] Windows경로의JSON조각이유효하지않음**
- 원인: mcp_panel.py:333서 경로를이스케이프없이문자열로이어붙임.
- 재현: `"C:\Program Files\EmailToMCP\EmailToMCP.exe"`→json.loads실패(Invalid \escape). `"C:\Users\UT\.venv\Scripts\python.exe" -m emailtomcp ...`→실패. `C:\new\tab\x.exe`→실패(관대한파서면\n,\t로오해석가능). macOS공백경로`/Applications/Email To MCP.app/...`→정상. 빈문자열→조각미표시(정상).
- 화면과저장파일에같은오류나감.
- 수정안: `json.dumps(exe)`·`json.dumps(args)`로값생성(인자공백있을때split()도shlex계열보강권장). 생성결과를`json.loads("{"+s+"}")`로확인하는테스트(Windows경로,공백경로케이스)추가.

**B-2[Medium,기능/안내] default아닌프로필발급시팝업명령에`--client <프로필>`빠짐**
- 팝업은"프로필'work'을발급했습니다.아래명령을실행하면연결됩니다"라안내하나, 명령은default프로필써서연결실패. 매뉴얼46-47행은수동으로덧붙이라되어있지만팝업문구와어긋남.
- 수정안: profile_name이default아니면명령·JSON args에`--client <name>`붙이거나, 팝업에수동추가안내표시.

**B-3[Low] 상태조회실패시등록명령이비어있는팝업이뜸**
- `refresh_status()`실패시조용히return해`snippet_edit`이빈상태로팝업열림.
- 수정안: 명령이비어있으면안내문구표시.

**B-4[Low,기존부채] 항목4의PlainText미지정라벨들**

## 요청
B-1(필수)과B-2(권장)를Edison에게수정위임. 수정후재검증예정.

## 관련 파일
`ui/dialogs/mcp_panel.py`, `app.py`, `mcp_server/auth.py`, `docs/manual/05_Claude_MCP패널.md`, `tests/ui/test_mcp_ui.py`
