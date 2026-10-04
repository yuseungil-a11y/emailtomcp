# EmailToMCP

로컬에서 실행되는 메일 클라이언트다. 내장 MCP 서버를 통해 Claude가 메일을 조회·작성·발송하고,
규칙에 따라 새 메일에 자동으로 답장한다. 자세한 설계는 `docs/DESIGN.md`를 참고한다.

> 현재 단계: **Phase 0(골격)**. GUI는 메뉴/툴바/폴더트리/그리드/미리보기 자리만 있는 빈 창이고,
> 메일 송수신·MCP 서버·자동회신은 이후 Phase(P1~P3)에서 채운다.

## 요구 사항

- Python 3.12 이상
- Windows 10/11 x64 또는 macOS 12 이상

## 개발 환경 준비

```powershell
# Windows (PowerShell)
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

```bash
# macOS/Linux
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

## 실행

```bash
python -m emailtomcp            # GUI 실행
python -m emailtomcp --version  # 버전만 출력하고 종료
python -m emailtomcp --help     # 사용법
python -m emailtomcp --data-dir "C:\temp\emailtomcp-data"  # 데이터 디렉터리 오버라이드
```

데이터 디렉터리는 `EMAILTOMCP_DATA_DIR` 환경변수로도 오버라이드할 수 있다(테스트·CI에서 주로 사용).
기본 위치는 `platformdirs.user_data_dir("EmailToMCP", "UTInfo")`이고, OS별 표준 경로를 따른다
(Windows: `%LOCALAPPDATA%\UTInfo\EmailToMCP`, macOS: `~/Library/Application Support/EmailToMCP`).

## 테스트

```bash
# PySide6는 헤드리스 환경에서 offscreen 플랫폼 플러그인이 필요하다.
set QT_QPA_PLATFORM=offscreen   # Windows cmd
$env:QT_QPA_PLATFORM="offscreen" # PowerShell
export QT_QPA_PLATFORM=offscreen # macOS/Linux

pytest                 # 기본: docker/e2e_claude/slow 마커 제외하고 실행
pytest -m unit          # 단위 테스트만
pytest -m ui            # pytest-qt UI 스모크만
```

마커 정의는 `pyproject.toml`의 `[tool.pytest.ini_options]`를 참고한다
(`unit`/`integration`/`docker`/`ui`/`e2e_claude`/`slow`).

## 린트/포맷/타입체크

```bash
ruff check .
ruff format --check .
mypy src
```

## 패키징(PyInstaller, onedir)

```bash
pip install pyinstaller
pyinstaller packaging/emailtomcp.spec
dist/emailtomcp/emailtomcp --version   # 빌드 산출물 스모크 확인
```

## 폴더 구조

```
src/emailtomcp/
├─ __main__.py        # CLI 진입점 (--version/--help는 Qt/DB 초기화 전에 처리)
├─ app.py              # 단일 인스턴스, 로깅, DB 마이그레이션, Backend 시작, MainWindow
├─ config/             # paths.py(경로), settings.py(settings 테이블 래퍼)
├─ core/               # errors.py, models.py(StrEnum), events.py, clock.py — PySide6 금지
├─ storage/            # db.py(단일 writer), migrations/, transitions.py, rebuild_table.py
├─ runtime/            # backend.py(asyncio 루프 스레드, PySide6 금지), qt_bridge.py(Qt 어댑터)
└─ ui/                 # main_window.py, resources/themes/{light,dark}.qss
tests/
├─ unit/   ui/         # pytest, pytest-qt(offscreen)
packaging/emailtomcp.spec
docs/                  # DESIGN.md, 리뷰 문서, UI 디자인 가이드
```

## 관련 문서

- `docs/DESIGN.md` — 아키텍처 설계(v0.1)
- `docs/reviews/` — 구조/보안/테스트/QA 리뷰
- `docs/design/UI_디자인가이드.md` — 색상·타이포그래피 토큰과 QSS 적용 지침
