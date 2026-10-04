"""update 패키지 — GitHub 기반 자동 업데이트(서명 검증, 확인, 알림), DESIGN.md §14~15.

P0에서는 트리 골격만 둔다. 실제 구현(version.py, manifest.py, verifier.py, keys.py,
release_client.py, checker.py, state.py)은 P1에서 채우고, 무인 설치(installer.py,
velopack_bridge.py)는 U2(P5)에서 채운다.

의존 규칙(DESIGN.md §4.2): core, config, storage(repositories), secrets에만 의존한다.
PySide6, runtime, ui, mcp_server는 import하지 않는다(아키텍처 테스트로 강제,
tests/unit/test_architecture_no_qt_in_backend.py).
"""
