"""순차 적용되는 마이그레이션 모음.

파일마다 `m{NNNN}_{설명}.py` 이름을 쓰고 `upgrade(conn: sqlite3.Connection) -> None`을 둔다.
실제 적용 순서와 "앱이 아는 최대 버전" 판단은 `runner.py`가 담당한다.
"""
