from __future__ import annotations

import os

# pytest-qt가 QApplication을 만들기 전에 반드시 설정돼야 한다(헤드리스 CI/로컬 모두 동일).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
