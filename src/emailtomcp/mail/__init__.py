"""mail 패키지 — 수신/발신/MIME/초안 서비스 (DESIGN.md §4.1).

P0에서는 트리 골격만 둔다. 실제 구현(incoming/, outgoing/, mime/, draft_service.py,
sync_service.py, send_service.py 등)은 P1~P3에서 채운다.

의존 규칙(DESIGN.md §4.2): core, config, storage(repositories), secrets에만 의존한다.
PySide6, runtime, ui, mcp_server는 import하지 않는다(아키텍처 테스트로 강제,
tests/unit/test_architecture_no_qt_in_backend.py).
"""
