"""autoreply 패키지 — `claude -p` 실행, 잡 큐, 사후 검증, 출력 가드 (DESIGN.md §2(c), §4.1).

P0에서는 트리 골격만 둔다. 실제 구현(cli_locator.py, job_queue.py, claude_runner.py,
env_policy.py, job_dir.py, prompts.py, reply_policy.py, output_guard.py,
stream_verifier.py)은 P3에서 채운다.

의존 규칙(DESIGN.md §4.2): core, config, storage(repositories), secrets에만 의존한다.
PySide6, runtime, ui, mcp_server는 import하지 않는다 — `mcp_server`와의 순환 의존은
`core.ports.JobTokenIssuer`를 거쳐 끊는다(S-07). 아키텍처 테스트로 강제한다
(tests/unit/test_architecture_no_qt_in_backend.py).
"""
