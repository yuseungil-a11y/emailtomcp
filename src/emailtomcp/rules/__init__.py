"""rules 패키지 — 자동회신 규칙 평가(schema/engine/loop_guard/auth_gate/rate_limit/regex_safe).

P0에서는 트리 골격만 둔다. 실제 구현은 P3에서 채운다.

의존 규칙(DESIGN.md §4.2): core, config, storage(repositories), secrets에만 의존한다.
PySide6, runtime, ui, mcp_server는 import하지 않는다(아키텍처 테스트로 강제,
tests/unit/test_architecture_no_qt_in_backend.py).
"""
