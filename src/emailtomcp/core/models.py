"""상태값 열거형.

모두 `enum.StrEnum`으로 정의하고, DB CHECK 제약의 허용값과 1:1로 맞춘다
(소크라테스 §4.1, P0 체크리스트, DESIGN.md v0.2 §5.1/§5.2). 스키마를 바꿀 때는 이 파일과
`storage/migrations/m0001_init.py`를 함께 수정해야 한다.
"""

from __future__ import annotations

from enum import StrEnum


class IncomingProtocol(StrEnum):
    """accounts.incoming_protocol"""

    POP3 = "pop3"
    IMAP = "imap"


class SecurityMode(StrEnum):
    """accounts.in_security / accounts.out_security"""

    SSL = "ssl"
    STARTTLS = "starttls"
    NONE = "none"


class AuthMethod(StrEnum):
    """accounts.auth_method"""

    PASSWORD = "password"
    OAUTH2 = "oauth2"


class FolderRole(StrEnum):
    """folders.role"""

    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    JUNK = "junk"
    ARCHIVE = "archive"
    CUSTOM = "custom"


class BodySource(StrEnum):
    """messages.body_source"""

    PLAIN = "plain"
    HTML = "html"


class AuthVerdict(StrEnum):
    """messages.auth_verdict (NULL 허용)"""

    PASS = "pass"
    FAIL = "fail"
    NONE = "none"
    UNTRUSTED = "untrusted"


class MessageAutoReplyStatus(StrEnum):
    """messages.auto_reply_status (NULL 허용, §5.2 상태값 일원화)"""

    IGNORED = "ignored"
    BLOCKED = "blocked"
    QUEUED = "queued"
    RUNNING = "running"
    SENT = "sent"
    DRAFTED = "drafted"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DraftKind(StrEnum):
    """drafts.kind"""

    NEW = "new"
    REPLY = "reply"
    REPLY_ALL = "reply_all"
    FORWARD_INLINE = "forward_inline"
    FORWARD_ATTACH = "forward_attach"


class DraftOrigin(StrEnum):
    """drafts.origin"""

    USER = "user"
    MCP = "mcp"
    AUTOREPLY = "autoreply"
    ASSIST = "assist"  # 수동 초안/요약 제안


class DraftStatus(StrEnum):
    """drafts.status"""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    OUTBOX = "outbox"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SEND_UNKNOWN = "send_unknown"


class ApprovedBy(StrEnum):
    """drafts.approved_by (NULL 허용)"""

    UI = "ui"
    POLICY_ALLOWLIST = "policy_allowlist"
    POLICY_AUTO_SEND = "policy_auto_send"
    USER_SEND = "user_send"


class DraftRecipientKind(StrEnum):
    """draft_recipients.kind"""

    TO = "to"
    CC = "cc"
    BCC = "bcc"


class RuleAction(StrEnum):
    """rules.action"""

    AUTO_SEND = "auto_send"
    DRAFT = "draft"
    IGNORE = "ignore"


class RuleReplyMode(StrEnum):
    """rules.reply_mode"""

    GENERATED = "generated"
    FIXED_TEMPLATE = "fixed_template"


class JobKind(StrEnum):
    """auto_reply_jobs.kind"""

    AUTO_REPLY = "auto_reply"
    MANUAL_DRAFT = "manual_draft"
    SUMMARY = "summary"


class JobPlannedAction(StrEnum):
    """auto_reply_jobs.planned_action"""

    AUTO_SEND = "auto_send"
    DRAFT = "draft"
    NONE = "none"


class JobStatus(StrEnum):
    """auto_reply_jobs.status"""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobOutcome(StrEnum):
    """auto_reply_jobs.outcome (NULL 허용)"""

    SENT = "sent"
    DRAFTED = "drafted"
    SKIPPED = "skipped"
    SUMMARIZED = "summarized"
    DISCARDED = "discarded"


class JobErrorClass(StrEnum):
    """auto_reply_jobs.error_class (NULL 허용), core.errors의 예외 분류와 1:1(§4.4)"""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    AUTH = "auth"
    POLICY = "policy"
    SECURITY = "security"


class AutoReplyOutcome(StrEnum):
    """auto_reply_log.outcome"""

    SENT = "sent"
    DRAFTED = "drafted"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class McpTokenKind(StrEnum):
    """mcp_tokens.kind"""

    PROXY = "proxy"
    HTTP = "http"


class McpAuditPrincipal(StrEnum):
    """mcp_audit.principal"""

    INTERACTIVE = "interactive"
    JOB = "job"


class McpAuditResult(StrEnum):
    """mcp_audit.result"""

    OK = "ok"
    DENIED = "denied"
    ERROR = "error"
