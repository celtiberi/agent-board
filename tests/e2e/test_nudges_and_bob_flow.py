"""End-to-end delivery tests: coalescing, HMAC, retries, auto-disable, crash recovery, and Bob wake flow."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import httpx2
import jsonschema
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_board.app import create_app
from agent_board.ids import dt_to_iso, new_ulid, utc_now
from agent_board.nudger.signing import verify_nudge_signature
from agent_board.nudger.worker import NudgeWorker
from agent_board.services import delivery as delivery_svc
from agent_board.services import messages as messages_svc
from tests.conftest import BoardTestEnv

_NUDGE_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schemas" / "api" / "nudge_payload.json").read_text(encoding="utf-8")
)


async def test_coalescing_hmac_and_cursor_suppression(env: BoardTestEnv) -> None:
    received: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        received.append(
            {
                "auth": request.headers.get("authorization"),
                "nudge_id": request.headers.get("x-board-nudge-id"),
                "timestamp": request.headers.get("x-board-timestamp"),
                "signature": request.headers.get("x-board-signature"),
                "raw_body": request.content,
                "json": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={"ok": True})

    sub = delivery_svc.create_subscription(
        env.services,
        env.bob_ctx,
        url="https://grokbot.example/routine/abc",
        boards=["alpha"],
        filter_cfg={
            "types": ["task", "question", "decision", "issue", "retest_report", "status"],
            "mentions_only": False,
            "exclude_self": True,
        },
        auth_header="Authorization: Bearer grok-routine-secret",
        hmac_secret="bob-shared-hmac",
        min_interval_s=60,
        debounce_s=10,
    )

    # 1. Plain note without @bob does NOT enqueue; 5 matching posts within 10s coalesce into 1 nudge
    messages_svc.post_message(env.services, env.dev_ctx, board_slug="alpha", msg_type="note", body_md="Quiet note")
    for idx in range(5):
        messages_svc.post_message(
            env.services,
            env.dev_ctx,
            board_slug="alpha",
            msg_type="retest_report",
            body_md=f"Retest batch {idx} secret_marker_do_not_leak",
            data={"target": "staging", "passed": idx, "failed": 0, "issue_ids": []},
        )

    worker = NudgeWorker(env.services, transport=httpx.MockTransport(_handler))
    t0 = utc_now()
    # Before 10s debounce elapses -> nothing sent
    assert await worker.process_due_once(now=t0 + timedelta(seconds=5)) == 0
    # After 10s debounce -> exactly 1 coalesced nudge with latest_seq == 6
    assert await worker.process_due_once(now=t0 + timedelta(seconds=12)) == 1
    assert len(received) == 1

    nudge = received[0]
    assert nudge["auth"] == "Bearer grok-routine-secret"
    jsonschema.validate(nudge["json"], _NUDGE_SCHEMA)
    assert nudge["json"]["board"] == "alpha"
    assert nudge["json"]["latest_seq"] == 6
    assert nudge["json"]["unread_for_you"] == 6
    assert b"secret_marker_do_not_leak" not in nudge["raw_body"]

    send_ts = int(nudge["timestamp"])
    assert verify_nudge_signature(
        "bob-shared-hmac",
        timestamp_header=nudge["timestamp"],
        signature_header=nudge["signature"],
        raw_body=nudge["raw_body"],
        now_ts=send_ts + 10,
    )
    # Timestamp older than 5 minutes must be rejected
    assert not verify_nudge_signature(
        "bob-shared-hmac",
        timestamp_header=nudge["timestamp"],
        signature_header=nudge["signature"],
        raw_body=nudge["raw_body"],
        now_ts=send_ts + 301,
    )

    # 2. If Bob reads & acks before the next nudge window fires, the pending nudge is suppressed
    messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="question",
        body_md="@bob quick check?",
        data={"for": "bob"},
    )
    delivery_svc.ack_cursor(env.services, env.bob_ctx, board_slug="alpha", read_seq=7)
    await worker.process_due_once(now=t0 + timedelta(seconds=90))
    assert len(received) == 1  # suppressed cleanly
    assert sub["id"]


async def test_retries_backoff_auto_disable_and_crash_recovery(env: BoardTestEnv) -> None:
    attempts = 0

    def _flaky_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            return httpx.Response(500, text="upstream down")
        return httpx.Response(200, json={"ok": True})

    sub = delivery_svc.create_subscription(
        env.services,
        env.bob_ctx,
        url="https://grokbot.example/flaky",
        boards=["alpha"],
        min_interval_s=0,
        debounce_s=0,
    )
    messages_svc.post_message(env.services, env.dev_ctx, board_slug="alpha", msg_type="task", body_md="Wake @bob")
    worker = NudgeWorker(env.services, transport=httpx.MockTransport(_flaky_handler))

    t = utc_now() + timedelta(seconds=1)
    assert await worker.process_due_once(now=t) == 1  # fail 1 -> +30s
    assert await worker.process_due_once(now=t + timedelta(seconds=20)) == 0
    assert await worker.process_due_once(now=t + timedelta(seconds=31)) == 1  # fail 2 -> +120s
    assert await worker.process_due_once(now=t + timedelta(seconds=155)) == 1  # fail 3 -> +600s
    assert await worker.process_due_once(now=t + timedelta(seconds=760)) == 1  # succeed 4
    assert attempts == 4

    # Crash recovery: stale 'sending' row alongside newer 'pending' row
    with env.services.db.write_tx() as conn:
        board_id = conn.execute("SELECT id FROM boards WHERE slug = 'alpha'").fetchone()["id"]
        stale_time = dt_to_iso(t + timedelta(seconds=800))
        conn.execute(
            """
            INSERT INTO outbox (
                id, subscription_id, board_id, latest_seq, attempt, next_attempt_at,
                status, locked_at, created_at
            )
            VALUES (?, ?, ?, 1, 0, ?, 'sending', ?, ?)
            """,
            (new_ulid(), sub["id"], board_id, stale_time, stale_time, stale_time),
        )
    messages_svc.post_message(
        env.services, env.dev_ctx, board_slug="alpha", msg_type="task", body_md="New msg during crash"
    )
    recovered_t = t + timedelta(seconds=900)
    assert await worker.process_due_once(now=recovered_t) == 1
    assert attempts == 5

    # 20 consecutive failures auto-disable subscription and post system messages to board + ops
    def _always_500(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="still broken")

    failing_worker = NudgeWorker(env.services, transport=httpx.MockTransport(_always_500))
    fail_sub = delivery_svc.create_subscription(
        env.services,
        env.bob_ctx,
        url="https://grokbot.example/dead",
        boards=["alpha"],
        min_interval_s=0,
        debounce_s=0,
    )
    messages_svc.post_message(
        env.services, env.dev_ctx, board_slug="alpha", msg_type="task", body_md="Trigger failures"
    )

    sim_t = recovered_t + timedelta(seconds=1)
    for _ in range(20):
        # Reset next_attempt_at so we don't wait hours across loop, while testing 20 failure count
        with env.services.db.write_tx() as conn:
            conn.execute(
                "UPDATE outbox SET next_attempt_at = ? WHERE subscription_id = ? AND status = 'pending'",
                (dt_to_iso(sim_t), fail_sub["id"]),
            )
        await failing_worker.process_due_once(now=sim_t)
        sim_t += timedelta(seconds=1)

    subs = delivery_svc.list_subscriptions(env.services, env.bob_ctx)
    dead_sub = next(s for s in subs if s["id"] == fail_sub["id"])
    assert dead_sub["active"] is False
    assert dead_sub["failure_count"] >= 20

    ops_msgs = messages_svc.read_messages(env.services, env.owner_ctx, board_slug="ops", since_seq=0)
    assert any(m["type"] == "system" and fail_sub["id"] in m["body_md"] for m in ops_msgs["messages"])


async def test_full_dev_ai_nudge_to_bob_mcp_reply_ack_flow(env: BoardTestEnv) -> None:
    nudges: list[dict[str, Any]] = []

    def _receiver(request: httpx.Request) -> httpx.Response:
        nudges.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    delivery_svc.create_subscription(
        env.services,
        env.bob_ctx,
        url="https://grokbot.example/wake",
        boards=["*"],
        min_interval_s=0,
        debounce_s=0,
    )

    # Dev AI posts a retest_report via REST
    app = create_app(env.services, enable_background_tasks=False)
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {env.dev_token}"},
        ) as dev_http:
            r = await dev_http.post(
                "/api/v1/boards/alpha/messages",
                json={
                    "thread_title": "retests",
                    "type": "retest_report",
                    "body_md": "Round 4 complete @bob — please verify",
                    "data": {"target": "staging", "passed": 12, "failed": 0, "issue_ids": []},
                },
            )
            assert r.status_code == 201
            posted_seq = r.json()["seq"]

        # Worker sends nudge to Bob
        worker = NudgeWorker(env.services, transport=httpx.MockTransport(_receiver))
        assert await worker.process_due_once(now=utc_now() + timedelta(seconds=1)) == 1
        assert len(nudges) == 1
        assert nudges[0]["board"] == "alpha"
        assert nudges[0]["mentioned"] is True

        # Bob wakes, reads via MCP, replies, and acks
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {env.bob_token}"},
        ) as bob_hc:
            async with streamable_http_client("http://testserver/mcp", http_client=bob_hc) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    msgs_res = await session.call_tool("read_messages", {"board": "alpha"})
                    msgs = json.loads(msgs_res.content[0].text)["messages"]
                    assert msgs[-1]["seq"] == posted_seq

                    await session.call_tool(
                        "post_message",
                        {
                            "board": "alpha",
                            "type": "status",
                            "thread_id": msgs[-1]["thread_id"],
                            "body_md": "Verified round 4.",
                            "data": {"state": "verified"},
                            "idempotency_key": "bob:retest:r4",
                        },
                    )
                    ack_res = await session.call_tool("ack", {"board": "alpha", "read_seq": posted_seq})
                    assert json.loads(ack_res.content[0].text)["read_seq"] == posted_seq
