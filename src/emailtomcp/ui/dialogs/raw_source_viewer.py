"""원문(.eml) 보기 다이얼로그 (DESIGN.md §11.1, §1.2 #21)."""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout, QWidget


class RawSourceViewer(QDialog):
    """메일 원문을 별도 창에 평문으로 보여준다. HTML을 렌더링하지 않는다(§8.4 안전)."""

    def __init__(self, raw: bytes, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("원문 보기")
        self.resize(700, 600)

        layout = QVBoxLayout(self)
        browser = QTextBrowser(self)
        browser.setOpenExternalLinks(False)
        browser.setOpenLinks(False)
        browser.setFont(QFont("Consolas"))
        browser.setPlainText(raw.decode("utf-8", errors="replace"))
        layout.addWidget(browser)
