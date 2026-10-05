# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 빌드 스펙 (P0 골격).

사용법: `pyinstaller packaging/emailtomcp.spec` (레포 루트에서 실행).
QSS 리소스(light.qss/dark.qss)와 앱 아이콘(app_icon.ico/.icns/.png)은 `datas`로 번들에
포함시켜, importlib.resources로 읽는 코드가 번들 안에서도 동작하게 한다.
Windows exe 자체의 아이콘은 EXE(icon=...)로 지정한다(작업표시줄/탐색기 표시용).
아이콘 소스: `packaging/icons/make_icon.py` (재생성: `python packaging/icons/make_icon.py`).

인앱 사용 설명서(`docs/manual/*.md`, DESIGN.md §11.9)는 번들 루트의 `docs/manual/`에
그대로 복사해 둔다. `ui/dialogs/manual_viewer.py`는 `sys._MEIPASS`(onedir에서도
PyInstaller가 번들 루트로 채워 준다) 기준으로 이 경로를 찾는다 — importlib.resources
패키지 리소스가 아니라 일반 데이터 파일로 다루는 이유는, 원본이 `src/emailtomcp/`
밖(`docs/manual/`)에 있어 패키지 네임스페이스로 만들지 않기 위함이다.
"""

from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 — PyInstaller가 주입하는 전역
SRC_ROOT = PROJECT_ROOT / "src"
THEMES_DIR = SRC_ROOT / "emailtomcp" / "ui" / "resources" / "themes"
ICONS_DIR = SRC_ROOT / "emailtomcp" / "ui" / "resources" / "icons"
MANUAL_DIR = PROJECT_ROOT / "docs" / "manual"

datas = [
    (str(THEMES_DIR / "light.qss"), "emailtomcp/ui/resources/themes"),
    (str(THEMES_DIR / "dark.qss"), "emailtomcp/ui/resources/themes"),
    (str(ICONS_DIR / "app_icon.ico"), "emailtomcp/ui/resources/icons"),
    (str(ICONS_DIR / "app_icon.icns"), "emailtomcp/ui/resources/icons"),
    (str(ICONS_DIR / "app_icon_256.png"), "emailtomcp/ui/resources/icons"),
    (str(ICONS_DIR / "app_icon_64.png"), "emailtomcp/ui/resources/icons"),
    (str(ICONS_DIR / "app_icon_32.png"), "emailtomcp/ui/resources/icons"),
]
datas += [(str(p), "docs/manual") for p in sorted(MANUAL_DIR.glob("*.md"))]

hiddenimports = [
    "emailtomcp",
    # MCP 서버(P2, §6.1): uvicorn은 프로토콜·lifespan 클래스를 문자열 경로로 import하므로
    # PyInstaller가 정적 분석으로 찾지 못한다. runtime/asgi_host.py가 쓰는 조합(http="h11",
    # lifespan="off", ws="none")과 로깅 모듈을 명시한다.
    "uvicorn.logging",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.lifespan.off",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    # 자동회신(P3): RE2 규칙 정규식(C 확장 `_re2`), 프로세스 트리 종료.
    "re2",
    "psutil",
]

a = Analysis(  # noqa: F821
    [str(SRC_ROOT / "emailtomcp" / "__main__.py")],
    pathex=[str(SRC_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="emailtomcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICONS_DIR / "app_icon.ico"),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="emailtomcp",
)
