"""update 패키지 — GitHub 기반 자동 업데이트(서명 검증, 확인, 알림), DESIGN.md §14~15.

P1 범위(확인 + 알림 + 검증된 릴리스 페이지 링크, §14.1):
- version.py: PEP 440 버전 파싱·비교(문자열 비교 금지)
- manifest.py: 서명 매니페스트 스키마(pydantic strict)
- keys.py: 내장 공개키 + minisign(Ed25519) 서명 검증 — 운영 키는 D-8 결정 후 교체(TODO)
- verifier.py: §14.4 클라이언트 검증 11단계
- release_client.py: HttpClient 구현(httpx + truststore, 64KB 상한)
- state.py: settings 테이블의 update.* 키
- checker.py: 확인 1회 실행 + 자동 확인 판단 + 이벤트 발행

무인 설치(installer.py, velopack_bridge.py)는 U2(P5)에서 채운다.

의존 규칙(DESIGN.md §4.2): core, config, storage(repositories), secrets에만 의존한다.
PySide6, runtime, ui, mcp_server는 import하지 않는다(아키텍처 테스트로 강제,
tests/unit/test_architecture_no_qt_in_backend.py).
"""
