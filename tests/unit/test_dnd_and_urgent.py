from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import jsonschema
import pytest
from fastapi.testclient import TestClient

from agent_board.app import create_app
from agent_board.errors import PermissionDeniedError, ValidationError
from agent_board.ids import dt_to_iso, utc_now
from agent_board.nudger.worker import NudgeWorker
from agent_board.services import brief as brief_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services import shout as shout_svc
from agent_board.store import delivery as delivery_store


def test_dnd_defers_non_urgent_and_urgent_plus_blocker_bypass(env) -> None:
    delivered: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True})

    worker = NudgeWorker(env.services, transport=httpx.MockTransport(handler))
    delivery_svc.create_subscription(
        env.services,
        env.bob_ctx,
        url="https://hooks.example.test/bob",
        boards=["*"],
        min_interval_s=0,
        debounce_s=0,
    )

    with pytest.raises(ValidationError):
        delivery_svc.set_dnd(env.services, env.bob_ctx, enabled=True, duration_minutes=999)
    with pytest.raises(PermissionDeniedError):
        delivery_svc.set_dnd(env.services, env.ro_ctx, enabled=True, duration_minutes=15)

    dnd_res = delivery_svc.set_dnd(
        env.services,
        env.bob_ctx,
        enabled=True,
        duration_minutes=45,
        reason="Refactoring judge module",
    )
    assert dnd_res["dnd_active"] is True
    assert dnd_res["dnd_until"] is not None

    # Brief and members list expose active DND state
    brief = brief_svc.get_brief(env.services, env.dev_ctx, board_slug="alpha")
    by_handle = {m["handle"]: m for m in brief["members"]}
    assert by_handle["bob"]["dnd"]["active"] is True
    assert "@bob (DND)" in brief["markdown"]

    app = create_app(env.services, enable_background_tasks=False)
    client = TestClient(app)
    members_resp = client.get("/api/v1/boards/alpha/members", headers={"Authorization": f"Bearer {env.dev_token}"})
    assert members_resp.status_code == 200
    m_bob = {m["handle"]: m for m in members_resp.json()["members"]}["bob"]
    assert m_bob["dnd"]["active"] is True
    assert m_bob["dnd"]["reason"] == "Refactoring judge module"

    # Normal message is deferred while Bob is in DND
    m_normal = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md="Non-urgent check",
        to=["bob"],
    )
    assert m_normal["priority"] == "normal"
    msg_schema = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "api" / "message.json").read_text())
    jsonschema.validate(m_normal, msg_schema)

    import asyncio

    future_run = utc_now() + timedelta(seconds=5)
    asyncio.run(worker.process_due_once(now=future_run))
    assert delivered == []

    # Urgent P0 message overrides DND immediately and validates against nudge_payload.json
    m_urgent = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="task",
        priority="urgent",
        body_md="Production outage",
        to=["bob"],
    )
    assert m_urgent["priority"] == "urgent"
    asyncio.run(worker.process_due_once(now=future_run + timedelta(seconds=1)))
    assert len(delivered) == 1
    assert delivered[0]["urgent"] is True
    assert delivered[0]["reason"] == "urgent"
    nudge_schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" / "api" / "nudge_payload.json").read_text()
    )
    jsonschema.validate(delivered[0], nudge_schema)

    # Ack seq 2, queue another non-urgent message, then clear DND -> expedites immediately!
    delivery_svc.ack_cursor(env.services, env.bob_ctx, board_slug="alpha", read_seq=m_urgent["seq"])
    messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Waiting for DND off",
        to=["bob"],
    )
    asyncio.run(worker.process_due_once(now=future_run + timedelta(seconds=2)))
    assert len(delivered) == 1  # Still deferred

    cleared = client.post(
        "/api/v1/me/dnd",
        headers={"Authorization": f"Bearer {env.bob_token}"},
        json={"enabled": False},
    )
    assert cleared.status_code == 200
    assert cleared.json()["expedited_outbox"] >= 1

    asyncio.run(worker.process_due_once(now=utc_now() + timedelta(seconds=1)))
    assert len(delivered) == 2
    assert delivered[1]["urgent"] is False

    # Re-enable DND, then open a blocker issue assigned to bob -> bypasses DND automatically
    delivery_svc.ack_cursor(env.services, env.bob_ctx, board_slug="alpha", read_seq=delivered[1]["latest_seq"])
    delivery_svc.set_dnd(env.services, env.bob_ctx, enabled=True, duration_minutes=30)
    issues_svc.open_issue(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        title="P0 blocker",
        body_md="Prod DB down",
        severity="blocker",
        assignee="bob",
    )
    asyncio.run(worker.process_due_once(now=utc_now() + timedelta(seconds=1)))
    assert len(delivered) == 3
    assert delivered[2]["urgent"] is True
    assert delivered[2]["reason"] == "urgent"


def test_worker_defers_pending_row_if_dnd_enabled_after_enqueue(env) -> None:
    delivered: list[dict[str, object]] = []
    worker = NudgeWorker(
        env.services,
        transport=httpx.MockTransport(
            lambda req: delivered.append(json.loads(req.content)) or httpx.Response(200)  # type: ignore[arg-type]
        ),
    )
    delivery_svc.create_subscription(
        env.services,
        env.bob_ctx,
        url="https://hooks.example.test/bob",
        boards=["*"],
        min_interval_s=0,
        debounce_s=0,
    )
    messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="Ping before DND",
        to=["bob"],
    )
    # Enable DND after outbox row was already due
    future_until = dt_to_iso(utc_now() + timedelta(minutes=20))
    with env.services.db.write_tx() as conn:
        conn.execute("UPDATE principals SET dnd_until = ? WHERE handle = 'bob'", (future_until,))

    import asyncio

    asyncio.run(worker.process_due_once(now=utc_now() + timedelta(seconds=2)))
    assert delivered == []
    pending = env.services.db.read_conn().execute(
        "SELECT status, next_attempt_at FROM outbox WHERE status = 'pending'"
    ).fetchone()
    assert pending is not None
    assert pending["next_attempt_at"] == future_until


def test_silent_flag_persists_and_skips_nudge_unread_details(env) -> None:
    # 1. Explicit to=[] sets silent=True and does not generate wakes even for actionable types
    m_silent = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="status",
        body_md="Quiet closure",
        to=[],
    )
    assert m_silent["silent"] is True

    # 2. Untargeted post (to=None) keeps silent=False
    m_open = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md="Open task for subscribers",
        to=None,
    )
    assert m_open["silent"] is False

    # 3. Silent post with wake=False via shout persists silent=True
    shout_res = shout_svc.shout(
        env.services,
        env.owner_ctx,
        body_md="Quiet shout",
        all_boards=True,
        wake=False,
    )
    assert shout_res["deliveries"]

    # 4. get_nudge_unread_details ignores silent messages (even when mentions_only=False)
    conn = env.services.db.read_conn()
    board = conn.execute("SELECT id FROM boards WHERE slug = 'alpha'").fetchone()
    assert board is not None
    bob = conn.execute("SELECT id FROM principals WHERE handle = 'bob'").fetchone()
    assert bob is not None

    # Starting cursor at m_silent["seq"] - 1 sees only m_open (not m_silent or quiet shout)
    details = delivery_store.get_nudge_unread_details(
        conn,
        board_id=board["id"],
        principal_id=bob["id"],
        read_seq=m_silent["seq"] - 1,
        filter_cfg={"mentions_only": False, "types": ["status", "task", "note"]},
    )
    assert details["trigger_seqs"] == [m_open["seq"]]

    # If everything unread is silent, trigger_seqs is empty (unmentioned silent closures do not trigger)
    details_only_silent = delivery_store.get_nudge_unread_details(
        conn,
        board_id=board["id"],
        principal_id=bob["id"],
        read_seq=m_open["seq"],
        filter_cfg={"mentions_only": False, "types": ["status", "task", "note"]},
    )
    assert details_only_silent["trigger_seqs"] == []
