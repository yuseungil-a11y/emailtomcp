"""인앱 사용 설명서 뷰어 (DESIGN.md §11.9).

`docs/manual/*.md`(앱이 직접 작성한 신뢰 콘텐츠)를 Markdown→HTML 변환 후
`QTextBrowser`로 보여준다. 메일 미리보기(`SecureMailBrowser`)와 달리 이 콘텐츠는
비신뢰 입력(메일 본문)이 아니므로 §8.4의 `loadResource` 제한을 적용하지 않는다.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import markdown
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)

# DESIGN.md §11.9 대응표.
SECTION_FILES: dict[str, str] = {
    "00_인덱스": "00_인덱스.md",
    "01_메인창": "01_메인창.md",
    "02_작성창": "02_작성창.md",
    "03_계정설정": "03_계정설정.md",
    "04_규칙설정": "04_규칙설정.md",
    "05_Claude_MCP패널": "05_Claude_MCP패널.md",
    "06_발송승인": "06_발송승인.md",
    "07_설정": "07_설정.md",
}


def _manual_dir() -> Path:
    """`docs/manual` 디렉터리를 찾는다.

    - **PyInstaller 빌드(onedir)**: `packaging/emailtomcp.spec`의 `datas`가
      `docs/manual/*.md`를 번들 루트의 `docs/manual/`에 그대로 복사해 둔다.
      PyInstaller는 onedir·onefile 모두에서 `sys._MEIPASS`에 번들 루트(onedir는
      실행파일이 있는 바로 그 디렉터리라 추출 지연이 없다)를 넣어 주므로 그
      경로를 우선 확인한다.
    - **개발/테스트 환경**(소스 트리 레이아웃): 조상 디렉터리를 거슬러 올라가며
      `docs/manual`을 찾는다.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "docs" / "manual"
        if candidate.is_dir():
            return candidate
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "docs" / "manual"
        if candidate.is_dir():
            return candidate
    return here.parents[4] / "docs" / "manual"


class ManualViewer(QDialog):
    """메뉴 "도움말 > 사용 설명서"(F1) 또는 각 화면 `[?]`로 연다."""

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("사용 설명서")
        self.resize(760, 640)

        layout = QVBoxLayout(self)
        self._browser = QTextBrowser(self)
        self._browser.setOpenExternalLinks(False)
        self._browser.setOpenLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor_clicked)
        layout.addWidget(self._browser)

        self._manual_dir = _manual_dir()

    def show_section(self, section: str) -> None:
        self._load_file(SECTION_FILES.get(section, SECTION_FILES["00_인덱스"]))

    def _load_file(self, filename: str) -> None:
        path = self._manual_dir / filename
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("사용 설명서 파일을 찾을 수 없습니다: %s", path)
            self._browser.setPlainText(f"사용 설명서 파일을 찾을 수 없습니다: {path}")
            return
        self._browser.setHtml(markdown.markdown(text, extensions=["tables"]))

    def _on_anchor_clicked(self, url) -> None:  # noqa: ANN001 — QUrl
        target_name = Path(url.path() or url.toString()).name
        if target_name.endswith(".md"):
            self._load_file(target_name)
            return
        QDesktopServices.openUrl(url)
