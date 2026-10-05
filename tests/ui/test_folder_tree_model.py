from __future__ import annotations

import pytest

from emailtomcp.ui.models.folder_tree_model import FolderTreeModel


@pytest.mark.ui
def test_account_status_tooltip_escapes_server_error(qtbot) -> None:  # noqa: ARG001
    """N-5: IMAP 서버 오류 문자열이 그대로 들어와도 툴팁에서 HTML/리치텍스트로
    해석되지 않도록 escape한다(main_window.py:1074 -> folder_tree_model.py 경로)."""
    model = FolderTreeModel()
    model.add_account(1, "계정1")

    evil = '<img src=//evil/s/a.png> & "quoted"'
    model.set_account_status(1, "disconnected", evil)

    name_item = model._account_items[1]
    parent = name_item.parent() or model.invisibleRootItem()
    status_item = parent.child(name_item.row(), 1)
    assert status_item is not None
    tooltip = status_item.toolTip()
    assert "<img" not in tooltip
    assert "&lt;img" in tooltip
    assert "&amp;" in tooltip
