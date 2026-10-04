"""mcp_server 패키지 — 내장 MCP 서버(두 번째 진입점), DESIGN.md §6.

P0에서는 트리 골격만 둔다. 실제 구현(server.py, asgi_guard.py, auth.py, scopes.py,
tools_read.py, tools_draft.py, tools_send.py, tools_job.py, approval.py, audit.py,
stdio_proxy.py, local_handshake.py)은 P2에서 채운다.

의존 규칙(DESIGN.md §4.2): core, config, storage, mail(draft_service 등),
rules(rate_limit)에만 의존한다. autoreply, PySide6, runtime, ui는 import하지 않는다
(아키텍처 테스트로 강제, tests/unit/test_architecture_no_qt_in_backend.py).
"""
