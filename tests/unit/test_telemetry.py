from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from agent_board.app import create_app
from agent_board.ids import dt_to_iso
from agent_board.services import delivery as delivery_svc
from agent_board.services import messages as messages_svc
from agent_board.telemetry import (
    TelemetryThresholds,
    collect_antigravity_telemetry,
    dispatch_telemetry_alerts,
)


def _write_transcript(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_collect_telemetry_and_cooldown_alert_dedup(tmp_path: Path) -> None:
    now = datetime(2026, 11, 21, 20, 0, 0, tzinfo=UTC)
    brain_dir = tmp_path / "brain"
    active_conv_path = tmp_path / "active_conversations.json"
    active_conv_path.write_text(
        json.dumps(
            {
                "*:alpha-agent": {
                    "board": "*",
                    "handle": "alpha-agent",
                    "conversation_id": "conv-alpha-111",
                }
            }
        ),
        encoding="utf-8",
    )

    # Full chronological transcript starting 10h ago (resumed within 5h window)
    events = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "created_at": dt_to_iso(now - timedelta(hours=10)),
            "content": "Initial prompt that must never leak into telemetry",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "model": "gemini-4-neon-xhigh",
            "input_tokens": 18_000,
            "cache_read_tokens": 0,
            "output_tokens": 200,
            "created_at": dt_to_iso(now - timedelta(hours=10)),
        },
        # Resumed 10m ago via webhook -> idle TTL drop, then partial collapse, then model switch
        {
            "step_index": 2,
            "type": "SYSTEM_MESSAGE",
            "source": "SYSTEM",
            "created_at": dt_to_iso(now - timedelta(minutes=10)),
            "content": "Agent Board notification (webhook) for @alpha-agent on board alpha",
        },
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "model": "gemini-4-neon-xhigh",
            "input_tokens": 195_000,
            "cache_read_tokens": 0,
            "output_tokens": 500,
            "created_at": dt_to_iso(now - timedelta(minutes=10) + timedelta(seconds=5)),
        },
        {
            "step_index": 4,
            "type": "SYSTEM_MESSAGE",
            "source": "SYSTEM",
            "created_at": dt_to_iso(now - timedelta(minutes=9)),
            "content": "Agent Board notification (webhook) wake 2",
        },
        {
            "step_index": 5,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "model": "gemini-4-neon-xhigh",
            "input_tokens": 180_000,
            "cache_read_tokens": 18_000,  # Partial collapse (<75% of 195k and >20k uncached and <180s gap)
            "output_tokens": 400,
            "created_at": dt_to_iso(now - timedelta(minutes=8, seconds=50)),
        },
        {
            "step_index": 6,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "model": "gemini-4-hydrogen-xhigh",  # Model switch hard drop
            "input_tokens": 199_000,
            "cache_read_tokens": 0,
            "output_tokens": 300,
            "created_at": dt_to_iso(now - timedelta(minutes=8, seconds=20)),
        },
    ]
    _write_transcript(
        brain_dir / "conv-alpha-111" / ".system_generated" / "logs" / "transcript.jsonl",
        events,
    )

    thresholds = TelemetryThresholds(
        context_warn_tokens=150_000,
        context_critical_tokens=280_000,
        agent_uncached_5h=500_000,
        global_uncached_5h=5_000_000,
        unattended_webhook_5h=400_000,
        unattended_idle_minutes=60,
        partial_collapse_count_5h=1,
        partial_collapse_uncached_5h=100_000,
    )
    snap = collect_antigravity_telemetry(
        brain_dir,
        active_conv_path,
        host_id="test-macbook",
        now=now,
        thresholds=thresholds,
    )
    raw = json.dumps(snap)
    assert "must never leak" not in raw
    assert "conv-alpha-111" not in raw
    assert str(tmp_path) not in raw

    a = snap["agents"]["alpha-agent"]
    assert a["live_context_tokens"] == 199_000
    assert a["uncached_5h"] == 195_000 + 180_000 + 199_000
    assert a["webhook_uncached_5h"] == 195_000 + 180_000 + 199_000
    assert a["hard_drops_5h"]["idle_ttl"] == 1
    assert a["hard_drops_5h"]["model_switch"] == 1
    assert a["partial_collapses_5h"] == 1
    assert snap["thresholds"]["context_warn_tokens"] == 150_000
    assert TelemetryThresholds().context_warn_tokens == 150_000

    alert_kinds = {al["kind"] for al in snap["alerts"]}
    assert {
        "context_warn",
        "agent_uncached_5h",
        "unattended_webhook_drain",
        "partial_collapse_spike",
    } <= alert_kinds


def test_idle_wake_miss_metric_excludes_midloop_system_messages(tmp_path: Path) -> None:
    now = datetime(2026, 11, 21, 21, 0, 0, tzinfo=UTC)
    brain_dir = tmp_path / "brain"
    active_conv_path = tmp_path / "active_conversations.json"
    active_conv_path.write_text("{}", encoding="utf-8")

    # Sequence within <3m on same model:
    # 0->2: idle turn ended (no tool_calls), SYSTEM_MESSAGE wake -> partial collapse (miss)
    # 2->4: mid-loop turn (tool_calls non-empty), SYSTEM_MESSAGE arrives -> ignored by idle-wake metric even if partial
    # 4->6: idle turn ended (no tool_calls), SYSTEM_MESSAGE wake -> cr=0 (miss)
    # 6->8: idle turn ended (no tool_calls), SYSTEM_MESSAGE wake -> clean cache hit
    events = [
        {
            "step_index": 0,
            "type": "PLANNER_RESPONSE",
            "model": "gemini-4-neon-xhigh",
            "input_tokens": 10_000,
            "cache_read_tokens": 190_000,
            "created_at": dt_to_iso(now - timedelta(minutes=10)),
        },
        {
            "step_index": 1,
            "type": "SYSTEM_MESSAGE",
            "content": "Agent Board notification (webhook) wake 1",
            "created_at": dt_to_iso(now - timedelta(minutes=9)),
        },
        {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "model": "gemini-4-neon-xhigh",
            "input_tokens": 182_000,
            "cache_read_tokens": 18_000,
            "tool_calls": [{"name": "read_file"}],
            "created_at": dt_to_iso(now - timedelta(minutes=8, seconds=50)),
        },
        {
            "step_index": 3,
            "type": "SYSTEM_MESSAGE",
            "content": "Background task notification mid-loop",
            "created_at": dt_to_iso(now - timedelta(minutes=8, seconds=40)),
        },
        {
            "step_index": 4,
            "type": "PLANNER_RESPONSE",
            "model": "gemini-4-neon-xhigh",
            "input_tokens": 180_000,
            "cache_read_tokens": 20_000,
            "created_at": dt_to_iso(now - timedelta(minutes=8, seconds=30)),
        },
        {
            "step_index": 5,
            "type": "SYSTEM_MESSAGE",
            "content": "Agent Board notification (webhook) wake 2",
            "created_at": dt_to_iso(now - timedelta(minutes=8)),
        },
        {
            "step_index": 6,
            "type": "PLANNER_RESPONSE",
            "model": "gemini-4-neon-xhigh",
            "input_tokens": 200_000,
            "cache_read_tokens": 0,
            "created_at": dt_to_iso(now - timedelta(minutes=7, seconds=50)),
        },
        {
            "step_index": 7,
            "type": "SYSTEM_MESSAGE",
            "content": "Agent Board notification (webhook) wake 3",
            "created_at": dt_to_iso(now - timedelta(minutes=7)),
        },
        {
            "step_index": 8,
            "type": "PLANNER_RESPONSE",
            "model": "gemini-4-neon-xhigh",
            "input_tokens": 2_000,
            "cache_read_tokens": 200_000,
            "created_at": dt_to_iso(now - timedelta(minutes=6, seconds=50)),
        },
    ]
    _write_transcript(brain_dir / "conv-idle-test" / ".system_generated" / "logs" / "transcript.jsonl", events)

    snap = collect_antigravity_telemetry(brain_dir, active_conv_path, host_id="idle-host", now=now)
    g = snap["global"]
    assert g["idle_wake_total_5h"] == 3
    assert g["idle_wake_partial_5h"] == 1
    assert g["idle_wake_zero_5h"] == 1
    assert g["idle_wake_miss_rate_5h"] == 66.7
    assert g["partial_collapses_5h"] == 2


def test_telemetry_endpoint_alerts_cooldown_and_monitor_render(env, tmp_path: Path) -> None:
    app = create_app(env.services, enable_background_tasks=False)
    client = TestClient(app)

    delivery_svc.set_dnd(env.services, env.bob_ctx, enabled=True, duration_minutes=45, reason="Focused fix")
    messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="task",
        priority="urgent",
        body_md="Urgent P0 task",
        to=["bob"],
    )

    payload = {
        "host_id": "test-host",
        "collected_at": "2026-11-21T20:00:00Z",
        "thresholds": {"context_warn_tokens": 150000, "context_critical_tokens": 280000},
        "global": {
            "uncached_1h": 250000,
            "uncached_5h": 600000,
            "uncached_24h": 900000,
            "cache_read_5h": 1200000,
            "hit_rate_5h": 66.7,
            "webhook_uncached_5h": 500000,
            "partial_collapses_5h": 2,
            "idle_wake_total_5h": 10,
            "idle_wake_partial_5h": 2,
            "idle_wake_zero_5h": 1,
            "idle_wake_miss_rate_5h": 30.0,
            "sessions_scanned": 3,
        },
        "agents": {
            "bob": {
                "live_context_tokens": 210000,
                "uncached_5h": 450000,
                "hit_rate_5h": 72.5,
                "partial_collapses_5h": 2,
                "alerts": [
                    {
                        "kind": "context_warn",
                        "severity": "warning",
                        "message": "@bob bound chat reached 210k context tokens",
                    }
                ],
            }
        },
        "alerts": [
            {
                "key": "bob:context_warn",
                "severity": "warning",
                "handle": "bob",
                "kind": "context_warn",
                "message": "@bob bound chat reached 210k context tokens",
            }
        ],
    }

    # Non-owner denied
    assert (
        client.post("/api/v1/admin/telemetry", headers={"Authorization": f"Bearer {env.bob_token}"}, json=payload)
        .status_code
        == 403
    )

    # Owner upload + readback
    up = client.post(
        "/api/v1/admin/telemetry",
        headers={"Authorization": f"Bearer {env.owner_token}"},
        json=payload,
    )
    assert up.status_code == 200
    assert up.json()["ok"] is True

    get_resp = client.get("/api/v1/admin/telemetry", headers={"Authorization": f"Bearer {env.owner_token}"})
    assert get_resp.status_code == 200
    assert get_resp.json()["snapshots"][0]["host_id"] == "test-host"

    # Cooldown dedup for alert posting
    state_path = tmp_path / "alert_state.json"

    class _ClientAdapter:
        def __init__(self, tc: TestClient, tok: str) -> None:
            self.tc = tc
            self.tok = tok

        def post(self, path: str, **kwargs):
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {self.tok}"
            return self.tc.post(f"/api/v1{path}", headers=headers, **kwargs)

    adapter = _ClientAdapter(client, env.owner_token)
    now = datetime(2026, 11, 21, 20, 0, 0, tzinfo=UTC)
    sent1 = dispatch_telemetry_alerts(
        payload,
        client=adapter,
        alert_board="alpha",
        alert_to="bob",
        state_path=state_path,
        cooldown_hours=4.0,
        now=now,
    )
    assert len(sent1) == 1

    # Within 4h cooldown -> suppressed
    sent2 = dispatch_telemetry_alerts(
        payload,
        client=adapter,
        alert_board="alpha",
        alert_to="bob",
        state_path=state_path,
        cooldown_hours=4.0,
        now=now + timedelta(hours=1),
    )
    assert sent2 == []

    # After 4h cooldown -> fires again
    sent3 = dispatch_telemetry_alerts(
        payload,
        client=adapter,
        alert_board="alpha",
        alert_to="bob",
        state_path=state_path,
        cooldown_hours=4.0,
        now=now + timedelta(hours=5),
    )
    assert len(sent3) == 1

    # /monitor JSON & SSR HTML show telemetry, DND, and URGENT feed badges
    env.settings.web_auto_login_handle = env.owner_ctx.handle
    env.settings.web_trusted_ips = ("100.64.0.10",)
    trusted = TestClient(app, client=("100.64.0.10", 51000))
    mon_json = trusted.get("/monitor/data?hours=24").json()
    assert mon_json["telemetry"]["host_id"] == "test-host"
    assert mon_json["telemetry"]["thresholds"]["context_warn_tokens"] == 150000
    assert mon_json["telemetry"]["global"]["idle_wake_miss_rate_5h"] == 30.0
    bob_row = next(r for r in mon_json["agent_matrix"] if r["board_slug"] == "alpha" and r["handle"] == "bob")
    assert bob_row["dnd"]["active"] is True
    assert bob_row["telemetry"]["live_context_tokens"] == 210000
    assert any(m["priority"] == "urgent" for m in mon_json["feed"])

    mon_html = trusted.get("/monitor")
    assert mon_html.status_code == 200
    assert "Token &amp; Cache Telemetry" in mon_html.text
    assert "Idle-turn wake miss (5h):" in mon_html.text
    assert "30.0% (2p+1z/10)" in mon_html.text
    assert "210k ctx" in mon_html.text
    assert "DND until" in mon_html.text
    assert "URGENT" in mon_html.text
