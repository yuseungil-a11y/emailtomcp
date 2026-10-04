"""storage 패키지 — sqlite3를 직접 다루는 유일한 곳.

이 패키지 밖에서는 raw sqlite3 사용을 금지한다(소크라테스 §8 P0 체크리스트).
다른 패키지는 `storage.db.Database`가 제공하는 연결/writer를 통해서만 DB에 접근한다.
PySide6를 import하지 않는다(아키텍처 테스트로 강제).
"""
