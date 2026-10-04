"""runtime 패키지 — 백엔드 실행 인프라(asyncio 루프 스레드)와 Qt 연결 어댑터.

`backend.py`는 PySide6를 import하지 않는다(아키텍처 테스트로 강제). `qt_bridge.py`는
core.events를 구독해 Qt Signal로 재발행하는 유일한 Qt-aware 모듈이다.
"""
