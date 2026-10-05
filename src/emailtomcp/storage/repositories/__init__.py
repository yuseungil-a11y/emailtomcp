"""storage.repositories — DB 접근 단일 창구(계정/폴더/메일/첨부/초안) (DESIGN.md §4.1).

raw sqlite3는 storage/ 밖에서 쓰지 않는다는 원칙(소크라테스 §3.1)에 따라, `mail/`
패키지는 이 하위 패키지의 함수를 통해서만 DB를 읽고 쓴다. 쓰기는 모두
`storage.db.Database.writer`를 거친다.
"""
