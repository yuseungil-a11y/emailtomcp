"""평문(PlainText) 전용 메시지 상자 헬퍼 (보안검토 N-2).

`QMessageBox.warning()`/`information()` 같은 정적 함수는 텍스트 형식이 AutoText라서,
`Qt.mightBeRichText()`가 True로 보는 문자열(예: 메일 Reply-To에서 온
`"<img src=//evil/s/a.png>"@evil.com`)이 섞이면 리치텍스트로 해석해 원격/UNC 이미지 로드를
시도할 수 있다. 메일·서버·예외 메시지처럼 **바깥에서 온 문자열이 섞일 수 있는 메시지**는
반드시 이 헬퍼로 띄운다(고정 문구만 있는 메시지는 기존 정적 함수를 써도 된다).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget


def make_plain_message_box(
    parent: QWidget | None, title: str, text: str, icon: QMessageBox.Icon
) -> QMessageBox:
    """텍스트 형식을 PlainText로 고정한 메시지 상자를 만든다(띄우지는 않는다).

    형식을 먼저 PlainText로 고정한 뒤 텍스트를 넣는다(방어 심층, 보안검토 S-2) —
    `QMessageBox(icon, title, text, ...)` 생성자로 텍스트를 먼저 넣으면 잠깐 AutoText
    상태를 거치므로, 프로젝트의 다른 직접 생성 지점들과 같은 순서로 통일한다.
    """
    box = QMessageBox(icon, title, "", QMessageBox.StandardButton.Ok, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    return box


def plain_warning(parent: QWidget | None, title: str, text: str) -> None:
    make_plain_message_box(parent, title, text, QMessageBox.Icon.Warning).exec()


def plain_information(parent: QWidget | None, title: str, text: str) -> None:
    make_plain_message_box(parent, title, text, QMessageBox.Icon.Information).exec()
