# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 빌드 스펙 (P0 골격).

사용법: `pyinstaller packaging/emailtomcp.spec` (레포 루트에서 실행).
QSS 리소스(light.qss/dark.qss)는 `datas`로 번들에 포함시켜, importlib.resources로
읽는 `app._load_stylesheet()`가 번들 안에서도 동작하게 한다.
"""

from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 — PyInstaller가 주입하는 전역
SRC_ROOT = PROJECT_ROOT / "src"
THEMES_DIR = SRC_ROOT / "emailtomcp" / "ui" / "resources" / "themes"

datas = [
    (str(THEMES_DIR / "light.qss"), "emailtomcp/ui/resources/themes"),
    (str(THEMES_DIR / "dark.qss"), "emailtomcp/ui/resources/themes"),
]

hiddenimports = [
    "emailtomcp",
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
