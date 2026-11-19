"""Unit tests for append-only messages, edits, redaction, idempotency, schemas, and secrets."""

from __future__ import annotations

import pytest

from agent_board.errors import ConflictError, PermissionDeniedError, ValidationError
from agent_board.services import messages as messages_svc
from agent_board.services import search as search_svc
from tests.conftest import BoardTestEnv


def test_edit_chain_and_thread_snapshot_ordering(env: BoardTestEnv) -> None:
    m1 = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="First message v1 @mac-dev-ai",
        thread_title="design",
    )
    m2 = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Second message",
        thread_title="design",
    )

    m1_v2 = messages_svc.edit_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        message_id=m1["id"],
        body_md="First message v2 updated",
    )
    assert m1_v2["seq"] == 3
    assert m1_v2["root_id"] == m1["id"]
    assert m1_v2["root_seq"] == 1
    assert m1_v2["supersedes_id"] == m1["id"]
    assert m1_v2["is_edit"] is True

    # Cannot edit an already-superseded version
    with pytest.raises(ConflictError):
        messages_svc.edit_message(
            env.services,
            env.bob_ctx,
            board_slug="alpha",
            message_id=m1["id"],
            body_md="Trying stale edit",
        )

    # Other non-owner cannot edit someone else's message
    with pytest.raises(PermissionDeniedError):
        messages_svc.edit_message(
            env.services,
            env.dev_ctx,
            board_slug="alpha",
            message_id=m1_v2["id"],
            body_md="Unauthorized edit",
        )

    # Thread snapshot preserves root_seq ordering (m1 stayed above m2) and collapses to latest
    snap = messages_svc.get_thread_snapshot(env.services, env.owner_ctx, board_slug="alpha", thread_id=m1["thread_id"])
    assert [m["id"] for m in snap["messages"]] == [m1_v2["id"], m2["id"]]
    assert snap["messages"][0]["version_count"] == 2
    assert snap["messages"][0]["body_md"] == "First message v2 updated"

    # Stream read since seq=1 delivers edit event
    stream = messages_svc.read_messages(env.services, env.dev_ctx, board_slug="alpha", since_seq=1)
    assert [m["id"] for m in stream["messages"]] == [m2["id"], m1_v2["id"]]

    # Full history check
    hist = messages_svc.get_message_with_history(env.services, env.owner_ctx, board_slug="alpha", message_id=m1["id"])
    assert [v["body_md"] for v in hist["versions"]] == [
        "First message v1 @mac-dev-ai",
        "First message v2 updated",
    ]


def test_redaction_scrubs_entire_root_chain_and_fts(env: BoardTestEnv) -> None:
    m1 = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Leaked key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        thread_title="general",
    )
    assert m1["warnings"] == ["secret redacted"]
    assert "sk-proj-" not in m1["body_md"]
    assert "[REDACTED]" in m1["body_md"]
    assert search_svc.search(env.services, env.owner_ctx, query="ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")["total"] == 0

    m2 = messages_svc.edit_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        message_id=m1["id"],
        body_md="Edited away the secret",
    )

    with pytest.raises(PermissionDeniedError):
        messages_svc.redact_message(env.services, env.bob_ctx, board_slug="alpha", message_id=m2["id"], reason="oops")

    redacted = messages_svc.redact_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        message_id=m2["id"],
        reason="accidental API key",
    )
    assert "redacted by owner" in redacted["body_md"]

    hist = messages_svc.get_message_with_history(env.services, env.owner_ctx, board_slug="alpha", message_id=m1["id"])
    for version in hist["versions"]:
        assert "sk-proj-" not in version["body_md"]
        assert "Edited away" not in version["body_md"]
        assert version["redacted_at"] is not None

    hits = search_svc.search(env.services, env.owner_ctx, query="Edited")
    assert hits["total"] == 0


def test_idempotency_replay_conflict_and_per_author_isolation(env: BoardTestEnv) -> None:
    m_bob = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Same work unit",
        idempotency_key="work-1",
    )
    m_bob_replay = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Same work unit",
        idempotency_key="work-1",
    )
    assert m_bob_replay["id"] == m_bob["id"]
    assert m_bob_replay["seq"] == m_bob["seq"]

    with pytest.raises(ConflictError):
        messages_svc.post_message(
            env.services,
            env.bob_ctx,
            board_slug="alpha",
            msg_type="note",
            body_md="Different payload with same key",
            idempotency_key="work-1",
        )

    # Same key from a different author creates a separate message
    m_dev = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Same work unit",
        idempotency_key="work-1",
    )
    assert m_dev["id"] != m_bob["id"]
    assert m_dev["seq"] == 2


def test_schema_validation_and_secret_warnings(env: BoardTestEnv) -> None:
    with pytest.raises(ValidationError):
        messages_svc.post_message(
            env.services,
            env.bob_ctx,
            board_slug="alpha",
            msg_type="retest_report",
            body_md="Bad schema",
            data={"passed": "not-an-int"},
        )

    # Unknown extra keys are allowed
    ok = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="retest_report",
        body_md="Token stored in vault:prod-api-key",
        data={"target": "staging", "passed": 10, "failed": 0, "issue_ids": [], "custom": True},
    )
    assert ok["warnings"] == []
    assert ok["data"]["custom"] is True

    flagged = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Oops token=ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
    )
    assert flagged["warnings"] == ["secret redacted"]
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in flagged["body_md"]
    assert "[REDACTED]" in flagged["body_md"]


def test_mentions_ignore_inline_and_fenced_code_spans(env: BoardTestEnv) -> None:
    inline_only = messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="FYI: mention `@bob` in docs when handing off QA.",
    )
    assert inline_only["mentions"] == []

    fenced_only = messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Example snippet:\n```\n@bob status\n```",
    )
    assert fenced_only["mentions"] == []

    real_mention = messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Example `@all` ignored, real mention @bob kept.",
    )
    assert real_mention["mentions"] == ["bob"]
