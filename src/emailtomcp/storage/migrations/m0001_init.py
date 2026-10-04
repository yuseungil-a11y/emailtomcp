"""초기 스키마 (user_version=1).

DESIGN.md v0.2 §5.1 DDL을 그대로 옮긴 것이다. 주요 보강 사항(§5.1 본문 "삭제" 항목 참조):
- `send_log`(S-12), `prompt_templates`(S-16), `messages.is_deleted`(C-08)는 아예 두지 않는다
  (레이트리밋은 drafts/draft_recipients로 계산하고, 기본 템플릿은 코드 상수, 삭제는 휴지통
  폴더 이동 하나로 통일한다).
- `messages.auto_reply_status`에 CHECK(§5.2 상태값 일원화), `drafts.status`/`kind`/`origin`에
  CHECK를 걸고, 상태 전이는 `storage/transitions.py`의 단일 함수로만 한다(S-02).
- FTS5는 외부콘텐츠(external content) + `trigram` 토크나이저(S-13, §5.1)를 쓰고, 색인 대상은
  평문 주소 컬럼(from_plain/to_plain)과 subject/body_text다. 동기화 트리거는 C-10.
- 승인 스냅샷 해시(drafts.approval_hash), Outbox(drafts.status='outbox'), MCP 토큰
  테이블(mcp_tokens), 자동회신 레이트리밋 근거(draft_recipients)를 추가했다.
- 인덱스 보완(S-14): attachments(message_id), auto_reply_jobs(status,next_run_at) 등.

주의: `conn.executescript()`는 Python sqlite3 모듈에서 실행 전 pending 트랜잭션을 암묵적으로
커밋해 버리고, 스크립트 내 각 문장을 개별적으로 자동 커밋한다 — `runner.py`가 바깥에서 거는
`BEGIN ... COMMIT`을 무력화한다. 그래서 이 모듈은 문장을 리스트로 쪼개 `conn.execute()`를
하나씩 호출한다(트리거 본문처럼 내부에 세미콜론이 있어도 SQLite 입장에서는 하나의 완결된
최상위 문장이면 execute() 한 번으로 문제없이 실행된다).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

SCHEMA_STATEMENTS: list[str] = [
    # 비밀번호와 OAuth 토큰은 keyring에만 저장한다(평문 폴백 없음, DESIGN.md §8)
    """
    CREATE TABLE accounts (
      id INTEGER PRIMARY KEY,
      display_name TEXT NOT NULL,
      email_address TEXT NOT NULL,
      aliases_json TEXT NOT NULL DEFAULT '[]',
      sender_name TEXT,
      provider_preset TEXT,
      auth_method TEXT NOT NULL DEFAULT 'password' CHECK (auth_method IN ('password','oauth2')),
      incoming_protocol TEXT NOT NULL CHECK (incoming_protocol IN ('pop3','imap')),
      in_host TEXT NOT NULL, in_port INTEGER NOT NULL,
      in_security TEXT NOT NULL CHECK (in_security IN ('ssl','starttls','none')),
      in_username TEXT NOT NULL,
      out_host TEXT NOT NULL, out_port INTEGER NOT NULL,
      out_security TEXT NOT NULL CHECK (out_security IN ('ssl','starttls','none')),
      out_username TEXT, out_auth_same_as_in INTEGER NOT NULL DEFAULT 1,
      pop3_leave_on_server INTEGER NOT NULL DEFAULT 1,
      pop3_delete_after_days INTEGER,
      poll_interval_sec INTEGER NOT NULL DEFAULT 300,
      signature TEXT,
      trusted_authserv_id TEXT,
      auto_reply_enabled INTEGER NOT NULL DEFAULT 0,
      enabled INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE folders (
      id INTEGER PRIMARY KEY,
      account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      role TEXT CHECK (role IN ('inbox','sent','drafts','trash','junk','archive','custom')),
      remote_name TEXT, uidvalidity INTEGER, last_uid INTEGER,
      parent_id INTEGER REFERENCES folders(id),
      UNIQUE (account_id, name)
    )
    """,
    """
    CREATE TABLE messages (
      id INTEGER PRIMARY KEY,
      account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
      folder_id INTEGER NOT NULL REFERENCES folders(id),
      original_folder_id INTEGER REFERENCES folders(id),
      message_id_hdr TEXT, in_reply_to TEXT, references_hdr TEXT,
      thread_key TEXT, remote_uid TEXT,
      from_addr TEXT, from_name TEXT, from_count INTEGER NOT NULL DEFAULT 1, sender_addr TEXT,
      to_addrs TEXT, cc_addrs TEXT, bcc_addrs TEXT, reply_to TEXT,
      from_addr_norm TEXT,
      from_plain TEXT, to_plain TEXT, cc_plain TEXT,
      subject TEXT, snippet TEXT,
      body_text TEXT,
      body_source TEXT CHECK (body_source IN ('plain','html')),
      content_mismatch INTEGER NOT NULL DEFAULT 0,
      has_html INTEGER NOT NULL DEFAULT 0,
      date_hdr TEXT, received_at TEXT NOT NULL, size_bytes INTEGER,
      is_read INTEGER NOT NULL DEFAULT 0,
      is_flagged INTEGER NOT NULL DEFAULT 0,
      is_answered INTEGER NOT NULL DEFAULT 0,
      priority INTEGER,
      has_attachments INTEGER NOT NULL DEFAULT 0,
      auto_headers TEXT,
      auth_summary TEXT,
      auth_verdict TEXT CHECK (auth_verdict IS NULL OR auth_verdict IN
        ('pass','fail','none','untrusted')),
      eml_path TEXT NOT NULL,
      auto_reply_status TEXT CHECK (auto_reply_status IS NULL OR auto_reply_status IN
        ('ignored','blocked','queued','running','sent','drafted','skipped','failed','cancelled')),
      ai_summary TEXT, ai_summary_at TEXT,
      UNIQUE (account_id, folder_id, remote_uid)
    )
    """,
    "CREATE INDEX ix_msg_folder_date ON messages(folder_id, received_at DESC)",
    "CREATE INDEX ix_msg_folder_unread ON messages(folder_id, is_read)",
    "CREATE INDEX ix_msg_thread ON messages(account_id, thread_key)",
    "CREATE INDEX ix_msg_msgid ON messages(message_id_hdr)",
    "CREATE INDEX ix_msg_from ON messages(account_id, from_addr_norm)",
    "CREATE INDEX ix_msg_arstatus ON messages(auto_reply_status) "
    "WHERE auto_reply_status IS NOT NULL",
    # 3자 이상은 trigram, 2자 이하 질의는 LIKE 폴백(R7). SQLite 3.34 이상 필수(CI 확인, D-20)
    """
    CREATE VIRTUAL TABLE messages_fts USING fts5(
      subject, from_plain, to_plain, body_text,
      content='messages', content_rowid='id', tokenize='trigram'
    )
    """,
    # 동기화 트리거(C-10): 외부 콘텐츠 FTS 표준 패턴('delete' 명령 후 재삽입)
    """
    CREATE TRIGGER trg_messages_fts_ai AFTER INSERT ON messages BEGIN
      INSERT INTO messages_fts(rowid, subject, from_plain, to_plain, body_text)
      VALUES (new.id, new.subject, new.from_plain, new.to_plain, new.body_text);
    END
    """,
    """
    CREATE TRIGGER trg_messages_fts_ad AFTER DELETE ON messages BEGIN
      INSERT INTO messages_fts(messages_fts, rowid, subject, from_plain, to_plain, body_text)
      VALUES ('delete', old.id, old.subject, old.from_plain, old.to_plain, old.body_text);
    END
    """,
    """
    CREATE TRIGGER trg_messages_fts_au AFTER UPDATE OF subject, from_plain, to_plain, body_text
    ON messages BEGIN
      INSERT INTO messages_fts(messages_fts, rowid, subject, from_plain, to_plain, body_text)
      VALUES ('delete', old.id, old.subject, old.from_plain, old.to_plain, old.body_text);
      INSERT INTO messages_fts(rowid, subject, from_plain, to_plain, body_text)
      VALUES (new.id, new.subject, new.from_plain, new.to_plain, new.body_text);
    END
    """,
    """
    CREATE TABLE attachments (
      id INTEGER PRIMARY KEY,
      message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
      part_index TEXT NOT NULL, filename_raw TEXT, filename_safe TEXT,
      content_type TEXT, detected_type TEXT, size_bytes INTEGER, sha256 TEXT,
      content_id TEXT, is_inline INTEGER NOT NULL DEFAULT 0,
      is_dangerous INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX ix_att_msg ON attachments(message_id)",
    """
    CREATE TABLE drafts (
      id INTEGER PRIMARY KEY,
      account_id INTEGER NOT NULL REFERENCES accounts(id),
      kind TEXT NOT NULL CHECK (kind IN
        ('new','reply','reply_all','forward_inline','forward_attach')),
      source_message_id INTEGER REFERENCES messages(id),
      to_addrs TEXT, cc_addrs TEXT, bcc_addrs TEXT,
      subject TEXT, body_text TEXT, body_html TEXT, attachments_json TEXT,
      origin TEXT NOT NULL CHECK (origin IN ('user','mcp','autoreply','assist')),
      mcp_token_id INTEGER REFERENCES mcp_tokens(id),
      job_id INTEGER REFERENCES auto_reply_jobs(id),
      status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
        ('draft','pending_approval','outbox','sending','sent','failed','send_unknown')),
      approval_hash TEXT, approval_requested_at TEXT, approval_expires_at TEXT,
      approved_at TEXT,
      approved_by TEXT CHECK (approved_by IS NULL OR approved_by IN
        ('ui','policy_allowlist','policy_auto_send','user_send')),
      outbox_expires_at TEXT,
      message_id_hdr TEXT,
      send_attempts INTEGER NOT NULL DEFAULT 0, next_send_at TEXT,
      sent_at TEXT, sent_message_id INTEGER REFERENCES messages(id),
      last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_drafts_status_next ON drafts(status, next_send_at)",
    "CREATE INDEX ix_drafts_origin_sent ON drafts(origin, sent_at)",
    # 레이트리밋(M1), allowlist 전수 검사(H9), 자동완성의 근거
    """
    CREATE TABLE draft_recipients (
      draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
      kind TEXT NOT NULL CHECK (kind IN ('to','cc','bcc')),
      addr_norm TEXT NOT NULL, domain_norm TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_drec_addr ON draft_recipients(addr_norm)",
    "CREATE INDEX ix_drec_domain ON draft_recipients(domain_norm)",
    # auto_send 규칙의 발신자 화이트리스트 필수 조건은 rules/schema.py 저장 검증에서 강제한다(§7.6)
    """
    CREATE TABLE rules (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL,
      account_id INTEGER REFERENCES accounts(id),
      enabled INTEGER NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 100,
      conditions_json TEXT NOT NULL,
      action TEXT NOT NULL CHECK (action IN ('auto_send','draft','ignore')),
      reply_mode TEXT NOT NULL DEFAULT 'generated'
        CHECK (reply_mode IN ('generated','fixed_template')),
      fixed_template TEXT,
      extra_instructions TEXT,
      max_chars INTEGER NOT NULL DEFAULT 400 CHECK (max_chars BETWEEN 50 AND 2000),
      allow_thread_context INTEGER NOT NULL DEFAULT 0,
      accept_aligned_dkim INTEGER NOT NULL DEFAULT 0,
      cooldown_hours INTEGER,
      version INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )
    """,
    # 잡 없이 차단된 경우(LoopGuard 등)도 auto_reply_log에만 기록하고 잡은 만들지 않는다
    """
    CREATE TABLE auto_reply_jobs (
      id INTEGER PRIMARY KEY,
      kind TEXT NOT NULL CHECK (kind IN ('auto_reply','manual_draft','summary')),
      message_id INTEGER NOT NULL REFERENCES messages(id),
      rule_id INTEGER REFERENCES rules(id), rule_version INTEGER,
      planned_action TEXT NOT NULL CHECK (planned_action IN ('auto_send','draft','none')),
      downgrade_reason TEXT,
      sender_addr_norm TEXT, sender_domain TEXT,
      status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed','cancelled')),
      outcome TEXT CHECK (outcome IS NULL OR outcome IN
        ('sent','drafted','skipped','summarized','discarded')),
      attempts INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, next_run_at TEXT, started_at TEXT, finished_at TEXT,
      result_draft_id INTEGER REFERENCES drafts(id),
      error_class TEXT CHECK (error_class IS NULL OR error_class IN
        ('transient','permanent','auth','policy','security')),
      error TEXT
    )
    """,
    "CREATE INDEX ix_jobs_status_next ON auto_reply_jobs(status, next_run_at)",
    "CREATE INDEX ix_jobs_sender ON auto_reply_jobs(sender_addr_norm, created_at)",
    "CREATE INDEX ix_jobs_domain ON auto_reply_jobs(sender_domain, created_at)",
    "CREATE INDEX ix_jobs_msg ON auto_reply_jobs(message_id)",
    # 자동회신 결정 감사(M6). 잡 없이 차단된 경우도 기록
    """
    CREATE TABLE auto_reply_log (
      id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
      message_id INTEGER, job_id INTEGER, rule_id INTEGER, rule_version INTEGER,
      sender_addr_norm TEXT,
      outcome TEXT NOT NULL CHECK (outcome IN
        ('sent','drafted','skipped','blocked','failed','cancelled')),
      reason_code TEXT,
      authserv_id TEXT, auth_verdict TEXT,
      recipient_basis TEXT,
      validation_json TEXT,
      sent_message_id_hdr TEXT,
      claude_turns INTEGER, claude_cost_usd REAL,
      tools_used_json TEXT, body_len INTEGER, body_sha256 TEXT, duration_ms INTEGER
    )
    """,
    "CREATE INDEX ix_arlog_ts ON auto_reply_log(ts)",
    "CREATE INDEX ix_arlog_sender_ts ON auto_reply_log(sender_addr_norm, ts)",
    """
    CREATE TABLE pop3_uidl_seen (
      account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
      uidl TEXT NOT NULL, first_seen_at TEXT NOT NULL, PRIMARY KEY (account_id, uidl)
    )
    """,
    # 평문은 저장하지 않는다(proxy 토큰 평문은 keyring에만)
    """
    CREATE TABLE mcp_tokens (
      id INTEGER PRIMARY KEY, label TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('proxy','http')),
      scopes_json TEXT NOT NULL,
      token_sha256 TEXT NOT NULL UNIQUE,
      created_at TEXT NOT NULL, last_used_at TEXT, expires_at TEXT, revoked_at TEXT
    )
    """,
    """
    CREATE TABLE mcp_audit (
      id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
      principal TEXT NOT NULL CHECK (principal IN ('interactive','job')),
      token_id INTEGER, token_hash8 TEXT, job_id INTEGER,
      tool TEXT, args_masked_json TEXT,
      result TEXT NOT NULL CHECK (result IN ('ok','denied','error')),
      http_status INTEGER, detail TEXT
    )
    """,
    "CREATE INDEX ix_audit_ts ON mcp_audit(ts)",
    "CREATE INDEX ix_audit_token ON mcp_audit(token_id, ts)",
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)",
]


def upgrade(conn: sqlite3.Connection) -> None:
    """runner.py가 이미 걸어 둔 트랜잭션 안에서 실행된다 — 여기서 commit/rollback하지 않는다."""
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
