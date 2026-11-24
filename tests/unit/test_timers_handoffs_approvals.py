from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_board.app import create_app
from agent_board.errors import ConflictError, PermissionDeniedError, ValidationError
from agent_board.ids import dt_to_iso, utc_now
from agent_board.nudger.worker import NudgeWorker
from agent_board.services import boards as boards_svc
from agent_board.services import brief as brief_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import messages as messages_svc
from agent_board.services import timers as timers_svc


def test_durable_timer_fires_and_wakes_only_owner(env) -> None:
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
    delivery_svc.create_subscription(
        env.services,
        env.dev_ctx,
        url="https://hooks.example.test/dev",
        boards=["*"],
        min_interval_s=0,
        debounce_s=0,
    )

    with pytest.raises(ValidationError):
        timers_svc.create_timer(
            env.services,
            env.bob_ctx,
            board_slug="alpha",
            message="bad",
            cancel_on_reply=True,
            delay_minutes=5,
        )
    with pytest.raises(ValidationError):
        timers_svc.create_timer(env.services, env.bob_ctx, board_slug="alpha", message="bad")

    timer = timers_svc.create_timer(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        message="Check CI status on PR #42",
        delay_minutes=15,
    )
    assert timer["status"] == "pending"
    assert timer["principal_handle"] == "bob"

    # Non-owner cannot cancel Bob's timer
    with pytest.raises(PermissionDeniedError):
        timers_svc.cancel_timer(env.services, env.dev_ctx, board_slug="alpha", timer_id=timer["id"])

    # Before due_at -> does not fire
    asyncio.run(worker.process_due_once(now=utc_now() + timedelta(minutes=5)))
    assert delivered == []

    # At due_at -> fires targeted system reminder waking ONLY bob
    asyncio.run(worker.process_due_once(now=utc_now() + timedelta(minutes=16)))
    assert len(delivered) == 1
    assert delivered[0]["principal"] == "bob"
    assert delivered[0]["mentioned"] is True

    fired = timers_svc.list_timers(env.services, env.bob_ctx, board_slug="alpha", status="fired")
    assert len(fired) == 1
    assert fired[0]["id"] == timer["id"]
    assert fired[0]["fired_message_id"] is not None


def test_timer_cancel_on_reply_suppresses_wake(env) -> None:
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
    msg = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="question",
        thread_title="retest-check",
        body_md="Did the staging deploy pass?",
        to=["mac-dev-ai"],
    )
    timer = timers_svc.create_timer(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        message="Follow up if dev hasn't replied",
        delay_minutes=30,
        thread_id=msg["thread_id"],
        cancel_on_reply=True,
    )

    # Another participant replies in the watched thread before due_at
    reply = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="status",
        thread_id=msg["thread_id"],
        body_md="Staging deploy passed.",
        to=[],
    )
    assert reply["seq"] > timer["baseline_seq"]

    # Process due timers -> auto-cancelled, no reminder posted or delivered
    asyncio.run(worker.process_due_once(now=utc_now() + timedelta(minutes=31)))
    assert delivered == []
    cancelled = timers_svc.list_timers(env.services, env.bob_ctx, board_slug="alpha", status="cancelled")
    assert any(t["id"] == timer["id"] for t in cancelled)


def test_thread_handoff_surfaces_in_brief_and_clears(env) -> None:
    msg = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="task",
        thread_title="oauth-migration",
        body_md="Starting OAuth migration",
        to=["bob"],
    )
    updated = boards_svc.set_thread_handoff(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        thread_id=msg["thread_id"],
        handoff_md="Done: PKCE flow. Left: refresh-token rotation tests.",
    )
    assert updated["handoff_md"] == "Done: PKCE flow. Left: refresh-token rotation tests."
    assert updated["handoff_updated_by_handle"] == "mac-dev-ai"

    brief = brief_svc.get_brief(env.services, env.bob_ctx, board_slug="alpha")
    assert len(brief["thread_handoffs"]) == 1
    assert brief["thread_handoffs"][0]["thread_title"] == "oauth-migration"
    assert "## Active Thread Handoffs (1)" in brief["markdown"]
    assert "PKCE flow" in brief["markdown"]

    # Clear handoff
    cleared = boards_svc.set_thread_handoff(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        thread_id="oauth-migration",
        handoff_md="",
    )
    assert cleared["handoff_md"] is None
    brief2 = brief_svc.get_brief(env.services, env.bob_ctx, board_slug="alpha")
    assert brief2["thread_handoffs"] == []
    assert "## Active Thread Handoffs" not in brief2["markdown"]


def test_monitor_needs_input_queue_and_inline_reply(env) -> None:
    app = create_app(env.services, enable_background_tasks=False)
    env.settings.web_auto_login_handle = env.owner_ctx.handle
    env.settings.web_trusted_ips = ("100.64.0.10",)
    trusted = TestClient(app, client=("100.64.0.10", 51000))

    # Agent asks owner a question -> appears in needs_input
    q = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="question",
        thread_title="deploy-approval",
        body_md="Ready to ship v2.1 to prod — approve?",
        to=[env.owner_ctx.handle],
    )
    snap1 = trusted.get("/monitor/data?hours=24").json()
    assert snap1["summary"]["needs_input_count"] == 1
    assert snap1["needs_input"][0]["id"] == q["id"]
    assert snap1["needs_input"][0]["author_handle"] == "mac-dev-ai"

    html_resp = trusted.get("/monitor")
    assert html_resp.status_code == 200
    assert "Needs Your Input" in html_resp.text
    assert "Ready to ship v2.1 to prod" in html_resp.text

    # Extract CSRF token from monitor HTML and submit inline quick-reply
    marker = 'data-csrf="'
    idx = html_resp.text.index(marker) + len(marker)
    csrf = html_resp.text[idx : html_resp.text.index('"', idx)]

    reply_resp = trusted.post(
        "/monitor/reply",
        headers={"Accept": "application/json"},
        data={
            "csrf_token": csrf,
            "board_slug": "alpha",
            "thread_id": q["thread_id"],
            "reply_to_id": q["id"],
            "to_handle": "mac-dev-ai",
            "body_md": "Approved — go ahead and deploy.",
        },
    )
    assert reply_resp.status_code == 200
    assert reply_resp.json()["ok"] is True

    # Question is now answered -> disappears from needs_input
    snap2 = trusted.get("/monitor/data?hours=24").json()
    assert snap2["summary"]["needs_input_count"] == 0
    assert snap2["needs_input"] == []

    # Verify REST timer + handoff endpoints work end-to-end
    api = TestClient(app)
    due = dt_to_iso(utc_now() + timedelta(hours=2))
    t_resp = api.post(
        "/api/v1/boards/alpha/timers",
        headers={"Authorization": f"Bearer {env.bob_token}"},
        json={"message": "Check deploy", "due_at": due},
    )
    assert t_resp.status_code == 201
    tid = t_resp.json()["id"]
    bob_hdr = {"Authorization": f"Bearer {env.bob_token}"}
    assert api.get("/api/v1/boards/alpha/timers", headers=bob_hdr).status_code == 200
    assert api.delete(f"/api/v1/boards/alpha/timers/{tid}", headers=bob_hdr).status_code == 200

    h_resp = api.put(
        f"/api/v1/boards/alpha/threads/{q['thread_id']}/handoff",
        headers={"Authorization": f"Bearer {env.dev_token}"},
        json={"handoff_md": "Approved by owner; deploying v2.1."},
    )
    assert h_resp.status_code == 200
    assert h_resp.json()["handoff_md"] == "Approved by owner; deploying v2.1."


def test_redteam_regressions_isolation_system_replies_and_closed_threads(env) -> None:
    delivered: list[dict[str, object]] = []
    worker = NudgeWorker(
        env.services,
        transport=httpx.MockTransport(
            lambda req: delivered.append(json.loads(req.content)) or httpx.Response(200)  # type: ignore[arg-type]
        ),
    )
    delivery_svc.create_subscription(
        env.services,
        env.dev_ctx,
        url="https://hooks.example.test/dev",
        boards=["*"],
        min_interval_s=0,
        debounce_s=0,
    )

    # 1. Naive due_at rejected
    with pytest.raises(ValidationError):
        timers_svc.create_timer(
            env.services,
            env.bob_ctx,
            board_slug="alpha",
            message="naive",
            due_at="2026-12-01T12:00:00",
        )

    # 2. Closed thread rejected for timers and non-empty handoffs
    thr = boards_svc.create_thread(env.services, env.dev_ctx, board_slug="alpha", title="reminders")
    boards_svc.close_thread(env.services, env.dev_ctx, board_slug="alpha", thread_id=thr["id"], closed=True)
    with pytest.raises(ValidationError):
        timers_svc.create_timer(
            env.services,
            env.dev_ctx,
            board_slug="alpha",
            message="on closed",
            delay_minutes=5,
            thread_id=thr["id"],
        )
    with pytest.raises(ValidationError):
        boards_svc.set_thread_handoff(
            env.services,
            env.dev_ctx,
            board_slug="alpha",
            thread_id=thr["id"],
            handoff_md="should fail on closed thread",
        )

    # 3. Removed member timer + closed 'reminders' thread must NOT poison-pill NudgeWorker
    bob_timer = timers_svc.create_timer(
        env.services, env.bob_ctx, board_slug="alpha", message="bob removed", delay_minutes=5
    )
    dev_timer = timers_svc.create_timer(
        env.services, env.dev_ctx, board_slug="alpha", message="dev still active", delay_minutes=5
    )
    # Remove Bob from board 'alpha' after he set the timer
    with env.services.db.write_tx() as conn:
        conn.execute(
            "DELETE FROM memberships WHERE principal_id = (SELECT id FROM principals WHERE handle = 'bob')"
        )

    # Both timers become due while 'reminders' thread is closed: Bob's auto-cancels, Dev's creates a new open
    # 'reminders' thread and fires cleanly without starving outbox delivery!
    asyncio.run(worker.process_due_once(now=utc_now() + timedelta(minutes=6)))
    assert len(delivered) == 1
    assert delivered[0]["principal"] == "mac-dev-ai"

    # Cancelling an already-fired timer raises ConflictError
    with pytest.raises(ConflictError):
        timers_svc.cancel_timer(env.services, env.dev_ctx, board_slug="alpha", timer_id=dev_timer["id"])
    assert any(
        t["id"] == bob_timer["id"]
        for t in timers_svc.list_timers(
            env.services, env.owner_ctx, board_slug="alpha", status="cancelled", all_principals=True
        )
    )

    # 4. System messages in a thread do NOT falsely trigger cancel_on_reply
    watch_msg = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="question",
        thread_title="watch-sys",
        body_md="Waiting on answer",
        to=[],
    )
    t_first = timers_svc.create_timer(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        message="First check-in",
        delay_minutes=5,
        thread_id=watch_msg["thread_id"],
    )
    t_second = timers_svc.create_timer(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        message="Escalation if nobody replied",
        delay_minutes=10,
        thread_id=watch_msg["thread_id"],
        cancel_on_reply=True,
    )
    # Fire t_first -> posts a 'system' message into watch-sys
    asyncio.run(worker.process_due_once(now=utc_now() + timedelta(minutes=6)))
    assert any(
        t["id"] == t_first["id"]
        for t in timers_svc.list_timers(env.services, env.dev_ctx, board_slug="alpha", status="fired")
    )
    # Fire t_second -> must STILL fire (system message from t_first is ignored by cancel_on_reply)
    asyncio.run(worker.process_due_once(now=utc_now() + timedelta(minutes=11)))
    assert any(
        t["id"] == t_second["id"]
        for t in timers_svc.list_timers(env.services, env.dev_ctx, board_slug="alpha", status="fired")
    )

    # 5. Closed thread items are excluded from /monitor needs_input
    q2 = messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="question",
        thread_title="soon-closed",
        body_md="Owner approval?",
        to=[env.owner_ctx.handle],
    )
    boards_svc.close_thread(env.services, env.dev_ctx, board_slug="alpha", thread_id=q2["thread_id"], closed=True)
    app = create_app(env.services, enable_background_tasks=False)
    env.settings.web_auto_login_handle = env.owner_ctx.handle
    env.settings.web_trusted_ips = ("100.64.0.10",)
    trusted = TestClient(app, client=("100.64.0.10", 51000))
    snap = trusted.get("/monitor/data?hours=24").json()
    assert all(item["id"] != q2["id"] for item in snap["needs_input"])
