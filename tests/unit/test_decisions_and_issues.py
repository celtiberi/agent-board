"""Unit tests for Decisions log and Issues lifecycle."""

from __future__ import annotations

import pytest

from agent_board.errors import ConflictError, PermissionDeniedError, ValidationError
from agent_board.services import boards as boards_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from tests.conftest import BoardTestEnv


def test_decisions_lifecycle_and_permissions(env: BoardTestEnv) -> None:
    d1 = decisions_svc.record_decision(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title="Use UTC in DB",
        statement="Store all timestamps in UTC ISO-8601.",
        decided_by="owner",
        decided_on="2026-11-16",
        rationale="Avoid DST ambiguity.",
        tags=["db", "time"],
    )
    assert d1["ref"] == "D-1"
    assert d1["status"] == "active"

    d2 = decisions_svc.supersede_decision(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        old_ref="D-1",
        title="Use UTC ISO-8601 with Z",
        statement="Store all timestamps in UTC ISO-8601 with Z suffix; UI renders America/Chicago.",
        decided_by="owner",
        decided_on="2026-11-16",
        reason="Clarify display timezone",
    )
    assert d2["ref"] == "D-2"

    # Cannot supersede already-superseded D-1
    with pytest.raises(ConflictError):
        decisions_svc.supersede_decision(
            env.services,
            env.owner_ctx,
            board_slug="alpha",
            old_ref="D-1",
            title="Another",
            statement="Nope",
            decided_by="owner",
            decided_on="2026-11-16",
            reason="Should fail",
        )

    d1_detail = decisions_svc.get_decision(env.services, env.owner_ctx, board_slug="alpha", ref="D-1")
    assert d1_detail["decision"]["status"] == "superseded"
    assert d1_detail["decision"]["superseded_by_ref"] == "D-2"
    assert len(d1_detail["versions"]) == 2

    # Only owner can revoke
    with pytest.raises(PermissionDeniedError):
        decisions_svc.revoke_decision(
            env.services, env.bob_ctx, board_slug="alpha", ref="D-2", reason="Agent cannot revoke"
        )

    revoked = decisions_svc.revoke_decision(
        env.services, env.owner_ctx, board_slug="alpha", ref="D-2", reason="No longer needed"
    )
    assert revoked["status"] == "revoked"

    # Disable agents_may_record_decisions -> agent rejected, owner allowed
    boards_svc.update_board(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        settings={"agents_may_record_decisions": False},
    )
    with pytest.raises(PermissionDeniedError):
        decisions_svc.record_decision(
            env.services,
            env.bob_ctx,
            board_slug="alpha",
            title="Blocked",
            statement="Agent should fail",
            decided_by="owner",
            decided_on="2026-11-16",
        )

    d3 = decisions_svc.record_decision(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        title="Owner still allowed",
        statement="Owner can always record decisions.",
        decided_by="owner",
        decided_on="2026-11-16",
    )
    assert d3["ref"] == "D-3"

    md = decisions_svc.export_decisions_md(env.services, env.ro_ctx, board_slug="alpha")
    assert "<!-- generated from agent-board; do not edit -->" in md
    assert "D-3: Owner still allowed" in md
    assert "D-1" in md


def test_issues_lifecycle_and_retest_linkage(env: BoardTestEnv) -> None:
    iss = issues_svc.open_issue(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title="Duplicate permit rows",
        body_md="Permits are inserted twice on retry.",
        severity="high",
        assignee="mac-dev-ai",
        tags=["ingest"],
    )
    assert iss["ref"] == "I-1"
    assert iss["status"] == "open"
    assert iss["assignee_handle"] == "mac-dev-ai"

    # Cannot jump directly to resolved via update_issue
    with pytest.raises(ValidationError):
        issues_svc.update_issue(env.services, env.dev_ctx, board_slug="alpha", ref="I-1", status="resolved")

    prog = issues_svc.update_issue(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        ref="I-1",
        status="in_progress",
        comment="Investigating upsert key",
    )
    assert prog["status"] == "in_progress"

    # Closing requires >= 20 char resolution
    with pytest.raises(ValidationError):
        issues_svc.close_issue(env.services, env.dev_ctx, board_slug="alpha", ref="I-1", resolution="fixed it")

    closed = issues_svc.close_issue(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        ref="I-1",
        resolution="Added unique constraint on (county, permit_number) and upsert at ingest.",
        status="resolved",
    )
    assert closed["status"] == "resolved"
    assert closed["resolved_at"] is not None

    # Closed issue cannot be moved to in_progress via update_issue
    with pytest.raises(ValidationError):
        issues_svc.update_issue(env.services, env.dev_ctx, board_slug="alpha", ref="I-1", status="in_progress")

    reopened = issues_svc.reopen_issue(
        env.services, env.bob_ctx, board_slug="alpha", ref="I-1", reason="Still failed in QA round 5"
    )
    assert reopened["status"] == "open"
    assert reopened["resolution"] is None

    # Retest report attaches to issue detail
    messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="retest_report",
        body_md="QA round 5 finished.",
        data={"target": "staging", "build": "sha123", "passed": 9, "failed": 1, "issue_ids": ["I-1"]},
    )
    detail = issues_svc.get_issue_detail(env.services, env.owner_ctx, board_slug="alpha", ref="I-1")
    assert detail["issue"]["latest_retest"]["build"] == "sha123"
    assert [e["action"] for e in detail["events"]] == [
        "opened",
        "status_changed",
        "resolved",
        "reopened",
    ]


def test_decision_title_strips_redundant_prefix_only(env: BoardTestEnv) -> None:
    d1 = decisions_svc.record_decision(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        title="D-23: Use Postgres WAL",
        statement="Use WAL for durable checkpoints.",
        decided_by="owner",
        decided_on="2026-11-21",
    )
    assert d1["ref"] == "D-1"
    assert d1["title"] == "Use Postgres WAL"

    d2 = decisions_svc.supersede_decision(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        old_ref="D-1",
        title="d-9 — Use SQLite WAL mode",
        statement="Use SQLite WAL mode for embedded coordination.",
        decided_by="owner",
        decided_on="2026-11-21",
        reason="Simpler single-node deployment",
    )
    assert d2["ref"] == "D-2"
    assert d2["title"] == "Use SQLite WAL mode"

    d3 = decisions_svc.record_decision(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        title="D-16 (clarified): Keep YAML schema strict",
        statement="Preserve qualified prefix when caller notes clarification.",
        decided_by="owner",
        decided_on="2026-11-21",
    )
    assert d3["ref"] == "D-3"
    assert d3["title"] == "D-16 (clarified): Keep YAML schema strict"
