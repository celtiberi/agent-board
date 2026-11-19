"""Unit tests for Project Brief, cursors, FTS5 search safety, and attachments."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_board.errors import ValidationError
from agent_board.ids import dt_to_iso, utc_now
from agent_board.services import attachments as att_svc
from agent_board.services import boards as boards_svc
from agent_board.services import brief as brief_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import messages as messages_svc
from agent_board.services import search as search_svc
from tests.conftest import BoardTestEnv


def test_cursor_forward_only_and_unread_excludes_self(env: BoardTestEnv) -> None:
    m1 = messages_svc.post_message(env.services, env.bob_ctx, board_slug="alpha", msg_type="note", body_md="From bob")
    m2 = messages_svc.post_message(env.services, env.dev_ctx, board_slug="alpha", msg_type="note", body_md="From dev")
    assert m1["seq"] == 1 and m2["seq"] == 2

    # Bob has 1 unread (dev's m2; bob's own m1 excluded)
    brief = brief_svc.get_brief(env.services, env.bob_ctx, board_slug="alpha")
    assert brief["cursor"]["unread_count"] == 1

    # Ack moves forward, never back, and rejects > latest_seq
    with pytest.raises(ValidationError):
        delivery_svc.ack_cursor(env.services, env.bob_ctx, board_slug="alpha", read_seq=3)

    ack2 = delivery_svc.ack_cursor(env.services, env.bob_ctx, board_slug="alpha", read_seq=2)
    assert ack2["read_seq"] == 2
    assert ack2["unread_count"] == 0

    ack1 = delivery_svc.ack_cursor(env.services, env.bob_ctx, board_slug="alpha", read_seq=1)
    assert ack1["read_seq"] == 2


def test_brief_tasks_questions_and_40_decision_fallback(env: BoardTestEnv) -> None:
    task_msg = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md="Please run QA round 4",
        data={"assignee": "bob", "state": "open"},
    )
    q1 = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="question",
        body_md="Approve DNS change?",
        data={"for": "owner"},
    )
    q2 = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="question",
        body_md="Use Cloudflare R2?",
        data={"for": "owner"},
    )

    bob_brief = brief_svc.get_brief(env.services, env.bob_ctx, board_slug="alpha")
    assert len(bob_brief["open_tasks_for_caller"]) == 1

    pat_brief = brief_svc.get_brief(env.services, env.owner_ctx, board_slug="alpha")
    assert [q["id"] for q in pat_brief["unanswered_questions_for_caller"]] == [q1["id"], q2["id"]]

    # Answer q1 via reply_to_id and close task via status; close q2 via status
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Approved.",
        reply_to_id=q1["id"],
    )
    messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="status",
        body_md="QA done",
        data={"task_message_id": task_msg["id"], "state": "done"},
    )
    messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="status",
        body_md="Cancelled question",
        data={"target_message_id": q2["id"], "state": "cancelled"},
    )

    assert brief_svc.get_brief(env.services, env.bob_ctx, board_slug="alpha")["open_tasks_for_caller"] == []
    assert brief_svc.get_brief(env.services, env.owner_ctx, board_slug="alpha")["unanswered_questions_for_caller"] == []

    # Add 41 decisions -> titles-only fallback
    for i in range(41):
        decisions_svc.record_decision(
            env.services,
            env.owner_ctx,
            board_slug="alpha",
            title=f"Decision {i}",
            statement=f"Detailed architectural statement number {i} explaining policy.",
            decided_by="owner",
            decided_on="2026-11-16",
        )
    large_brief = brief_svc.get_brief(env.services, env.bob_ctx, board_slug="alpha")
    assert large_brief["decisions_titles_only"] is True
    assert "Showing decision titles only" in large_brief["markdown"]


def test_search_syntax_and_permission_isolation(env: BoardTestEnv) -> None:
    boards_svc.create_board(env.services, env.owner_ctx, slug="secret-board", name="Secret", purpose="Owner only")
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="secret-board",
        msg_type="note",
        body_md="Confidential hyphen-search-keyword inside secret board",
    )
    messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Public hyphen-search-keyword with schema validation details",
        thread_title="retests",
    )
    decisions_svc.record_decision(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title="Validation wording",
        statement="Always check hyphen-search-keyword before closing.",
        decided_by="owner",
        decided_on="2026-11-16",
    )

    # Bob only sees alpha, never secret-board, and hyphens/quotes don't crash FTS5
    res_hyphen = search_svc.search(env.services, env.bob_ctx, query="hyphen-search-keyword")
    assert res_hyphen["total"] == 3  # note + decision message + decision record
    assert {h["board_slug"] for h in res_hyphen["hits"]} == {"alpha"}
    assert {h["kind"] for h in res_hyphen["hits"]} == {"message", "decision"}

    res_prefix = search_svc.search(env.services, env.bob_ctx, query='valid* OR "schema validation"')
    assert res_prefix["total"] >= 2


def test_attachments_dedup_allowlist_and_orphan_gc(env: BoardTestEnv) -> None:
    with pytest.raises(ValidationError):
        att_svc.upload_attachment(
            env.services,
            env.bob_ctx,
            board_slug="alpha",
            filename="evil.html",
            content_type="text/html",
            content_bytes=b"<script>alert(1)</script>",
        )

    a1 = att_svc.upload_attachment(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        filename="../weird path/retest.log",
        content_type="text/x-log",
        content_bytes=b"line1\nline2\n",
    )
    a2 = att_svc.upload_attachment(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        filename="orphan.txt",
        content_type="text/plain",
        content_bytes=b"line1\nline2\n",
    )
    assert a1["sha256"] == a2["sha256"]
    assert a1["filename"] == "retest.log"

    messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Attached log",
        attachment_ids=[a1["id"]],
    )

    # Backdate orphan a2 to 25h ago and run GC; blob remains because a1 still references sha256
    old_iso = dt_to_iso(utc_now() - timedelta(hours=25))
    with env.services.db.write_tx() as conn:
        conn.execute("UPDATE attachments SET created_at = ? WHERE id = ?", (old_iso, a2["id"]))
    removed = att_svc.cleanup_orphan_attachments(env.services, max_age_hours=24)
    assert removed == 1
    meta, path = att_svc.get_attachment_for_download(env.services, env.ro_ctx, attachment_id=a1["id"])
    assert meta["filename"] == "retest.log"
    assert path.exists()


def test_brief_lists_members_and_tags(env: BoardTestEnv) -> None:
    import json
    from pathlib import Path

    import jsonschema

    from agent_board.services.auth import set_principal_tags

    set_principal_tags(env.services, env.owner_ctx, handle="bob", tags=["qa"])
    set_principal_tags(env.services, env.owner_ctx, handle="mac-dev-ai", tags=["dev", "infra"])

    brief = brief_svc.get_brief(env.services, env.bob_ctx, board_slug="alpha")
    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "api" / "brief.json").read_text())
    jsonschema.validate(brief, schema)

    by_handle = {m["handle"]: m for m in brief["members"]}
    assert by_handle["bob"]["tags"] == ["qa"]
    assert by_handle["mac-dev-ai"]["tags"] == ["dev", "infra"]
    assert "**Members:**" in brief["markdown"]
    assert "@bob [qa]" in brief["markdown"]
    assert "@mac-dev-ai [dev, infra]" in brief["markdown"]
