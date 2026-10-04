from __future__ import annotations

from emailtomcp.core import models


def test_incoming_protocol_matches_db_check() -> None:
    assert {m.value for m in models.IncomingProtocol} == {"pop3", "imap"}


def test_security_mode_matches_db_check() -> None:
    assert {m.value for m in models.SecurityMode} == {"ssl", "starttls", "none"}


def test_auth_method_matches_db_check() -> None:
    assert {m.value for m in models.AuthMethod} == {"password", "oauth2"}


def test_folder_role_matches_db_check() -> None:
    assert {m.value for m in models.FolderRole} == {
        "inbox",
        "sent",
        "drafts",
        "trash",
        "junk",
        "archive",
        "custom",
    }


def test_body_source_matches_db_check() -> None:
    assert {m.value for m in models.BodySource} == {"plain", "html"}


def test_auth_verdict_matches_db_check() -> None:
    assert {m.value for m in models.AuthVerdict} == {"pass", "fail", "none", "untrusted"}


def test_message_auto_reply_status_matches_db_check() -> None:
    assert {m.value for m in models.MessageAutoReplyStatus} == {
        "ignored",
        "blocked",
        "queued",
        "running",
        "sent",
        "drafted",
        "skipped",
        "failed",
        "cancelled",
    }


def test_draft_kind_matches_db_check() -> None:
    assert {m.value for m in models.DraftKind} == {
        "new",
        "reply",
        "reply_all",
        "forward_inline",
        "forward_attach",
    }


def test_draft_origin_matches_db_check() -> None:
    assert {m.value for m in models.DraftOrigin} == {"user", "mcp", "autoreply", "assist"}


def test_draft_status_matches_db_check() -> None:
    assert {m.value for m in models.DraftStatus} == {
        "draft",
        "pending_approval",
        "outbox",
        "sending",
        "sent",
        "failed",
        "send_unknown",
    }


def test_approved_by_matches_db_check() -> None:
    assert {m.value for m in models.ApprovedBy} == {
        "ui",
        "policy_allowlist",
        "policy_auto_send",
        "user_send",
    }


def test_draft_recipient_kind_matches_db_check() -> None:
    assert {m.value for m in models.DraftRecipientKind} == {"to", "cc", "bcc"}


def test_rule_action_matches_db_check() -> None:
    assert {m.value for m in models.RuleAction} == {"auto_send", "draft", "ignore"}


def test_rule_reply_mode_matches_db_check() -> None:
    assert {m.value for m in models.RuleReplyMode} == {"generated", "fixed_template"}


def test_job_kind_matches_db_check() -> None:
    assert {m.value for m in models.JobKind} == {"auto_reply", "manual_draft", "summary"}


def test_job_planned_action_matches_db_check() -> None:
    assert {m.value for m in models.JobPlannedAction} == {"auto_send", "draft", "none"}


def test_job_status_matches_db_check() -> None:
    assert {m.value for m in models.JobStatus} == {
        "queued",
        "running",
        "done",
        "failed",
        "cancelled",
    }


def test_job_outcome_matches_db_check() -> None:
    assert {m.value for m in models.JobOutcome} == {
        "sent",
        "drafted",
        "skipped",
        "summarized",
        "discarded",
    }


def test_job_error_class_matches_db_check() -> None:
    assert {m.value for m in models.JobErrorClass} == {
        "transient",
        "permanent",
        "auth",
        "policy",
        "security",
    }


def test_auto_reply_outcome_matches_db_check() -> None:
    assert {m.value for m in models.AutoReplyOutcome} == {
        "sent",
        "drafted",
        "skipped",
        "blocked",
        "failed",
        "cancelled",
    }


def test_mcp_token_kind_matches_db_check() -> None:
    assert {m.value for m in models.McpTokenKind} == {"proxy", "http"}


def test_mcp_audit_principal_matches_db_check() -> None:
    assert {m.value for m in models.McpAuditPrincipal} == {"interactive", "job"}


def test_mcp_audit_result_matches_db_check() -> None:
    assert {m.value for m in models.McpAuditResult} == {"ok", "denied", "error"}
