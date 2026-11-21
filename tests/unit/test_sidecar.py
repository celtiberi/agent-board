from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import httpx
import pytest

from agent_board.constants import ACTIONABLE_MESSAGE_TYPES_SET
from agent_board.nudger.signing import sign_nudge
from agent_board.sidecar import (
    ListenerState,
    ReusableThreadingHTTPServer,
    _build_prompt,
    _load_active_conversations,
    _lookup_route,
    _make_handler,
    _recover_one_board,
    _resolve_agentapi,
    _resolve_board_cli,
)


def test_sidecar_route_loading_and_lookup_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "agent-board"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "active_conversations.json").write_text(
        json.dumps(
            {
                "legacy-board": "conv-legacy-admin",
                "alpha:admin": {
                    "board": "alpha",
                    "handle": "admin",
                    "conversation_id": "conv-alpha-admin",
                    "subscription_id": "sub-alpha-admin",
                },
                "*:admin": {
                    "board": "*",
                    "handle": "admin",
                    "conversation_id": "conv-wild-admin",
                    "subscription_id": "sub-wild-admin",
                },
                "*:dev-agent": {
                    "board": "*",
                    "handle": "dev-agent",
                    "conversation_id": "conv-wild-dev",
                    "subscription_id": "sub-wild-dev",
                },
            }
        ),
        encoding="utf-8",
    )

    routes = _load_active_conversations()
    assert ("legacy-board", "admin") in routes
    assert routes[("*", "dev-agent")]["conversation_id"] == "conv-wild-dev"

    # Explicit (board, handle) takes precedence over wildcard.
    assert _lookup_route("alpha", "admin", "sub-wild-admin")["conversation_id"] == "conv-alpha-admin"  # type: ignore[index]
    # Wildcard matches new boards for handle or subscription_id.
    wild_match = _lookup_route("brand-new", "dev-agent", "sub-wild-dev")
    assert wild_match is not None
    assert wild_match["board"] == "brand-new"
    assert wild_match["conversation_id"] == "conv-wild-dev"
    # Subscription ID lookup works even without principal header.
    sub_match = _lookup_route("another-board", None, "sub-wild-dev")
    assert sub_match is not None
    assert sub_match["handle"] == "dev-agent"
    # Bare legacy board resolves cleanly.
    assert _lookup_route("legacy-board", None, None)["conversation_id"] == "conv-legacy-admin"  # type: ignore[index]
    # Unbound target returns None.
    assert _lookup_route("unknown", "stranger", "sub-unknown") is None


def test_listener_state_dedup_inflight_and_persistence(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state = ListenerState(state_file)

    assert state.should_enqueue("alpha", "dev-agent", 5, "nudge-1") is True
    # Concurrent same seq blocked while inflight.
    assert state.should_enqueue("alpha", "dev-agent", 5, "nudge-2") is False
    # Releasing allows retry.
    state.release_inflight("alpha", "dev-agent", 5, "nudge-1")
    assert state.should_enqueue("alpha", "dev-agent", 5, "nudge-1") is True

    state.mark_dispatched("alpha", "dev-agent", 5, "nudge-1")
    assert state.get_last_seq("alpha", "dev-agent") == 5
    # Stale seq or seen nudge_id stays suppressed.
    assert state.should_enqueue("alpha", "dev-agent", 5, "nudge-new") is False
    assert state.should_enqueue("alpha", "dev-agent", 6, "nudge-1") is False
    assert state.should_enqueue("alpha", "dev-agent", 6, "nudge-3") is True

    reloaded = ListenerState(state_file)
    assert reloaded.get_last_seq("alpha", "dev-agent") == 5


def test_recover_one_board_filters_mentions_notes_and_self(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    state = ListenerState(tmp_path / "state.json")

    def _mock_transport(messages: list[dict[str, object]]) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/boards/alpha":
                return httpx.Response(200, json={"unread_count": len(messages), "next_seq": 20})
            if request.url.path == "/api/v1/boards/alpha/messages":
                return httpx.Response(200, json={"messages": messages})
            return httpx.Response(404)

        return httpx.MockTransport(_handler)

    # 1. mentions_only=True ignores unmentioned/cc-only tasks and self messages, picks up explicit to=['dev-agent'].
    msgs_1 = [
        {"seq": 10, "author_handle": "dev-agent", "type": "task", "to": ["other"], "mentions": ["other"]},
        {"seq": 11, "author_handle": "owner", "type": "task", "to": [], "cc": ["dev-agent"], "mentions": []},
        {
            "seq": 12,
            "author_handle": "owner",
            "type": "note",
            "thread_title": "website-docs",
            "to": ["dev-agent"],
            "mentions": ["dev-agent"],
        },
    ]
    q1: queue.Queue[dict[str, object] | None] = queue.Queue()
    with httpx.Client(base_url="http://board.test/api/v1", transport=_mock_transport(msgs_1)) as c:
        _recover_one_board(
            c,
            state,
            q1,
            slug="alpha",
            handle="dev-agent",
            sub_id="sub-1",
            mentions_only=True,
            allowed_types=ACTIONABLE_MESSAGE_TYPES_SET,
        )
    item1 = q1.get_nowait()
    assert item1 is not None
    assert item1["latest_seq"] == 12
    assert item1["mentioned"] is True
    assert item1["trigger_seqs"] == [12]
    assert item1["trigger_threads"] == ["website-docs"]

    # 2. mentions_only=False ignores plain unmentioned notes, picks up unmentioned actionable types.
    state.mark_dispatched("alpha", "dev-agent", 12, "catchup-alpha-dev-agent-12")
    msgs_2 = [
        {"seq": 13, "author_handle": "owner", "type": "note", "mentions": []},
        {"seq": 14, "author_handle": "owner", "type": "issue", "mentions": []},
    ]
    q2: queue.Queue[dict[str, object] | None] = queue.Queue()
    with httpx.Client(base_url="http://board.test/api/v1", transport=_mock_transport(msgs_2)) as c:
        _recover_one_board(
            c,
            state,
            q2,
            slug="alpha",
            handle="dev-agent",
            sub_id="sub-1",
            mentions_only=False,
            allowed_types=ACTIONABLE_MESSAGE_TYPES_SET,
        )
    item2 = q2.get_nowait()
    assert item2 is not None
    assert item2["latest_seq"] == 14
    assert item2["mentioned"] is False


def test_webhook_handler_auth_hmac_and_wildcard_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "agent-board"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "active_conversations.json").write_text(
        json.dumps(
            {
                "*:qa-bot": {
                    "board": "*",
                    "handle": "qa-bot",
                    "conversation_id": "conv-qa-1",
                    "subscription_id": "sub-qa",
                }
            }
        ),
        encoding="utf-8",
    )

    state = ListenerState(tmp_path / "listener.json")
    work_q: queue.Queue[dict[str, object] | None] = queue.Queue()
    hook_cfg = {"bearer_token": "secret-bearer", "hmac_secret": "whsec-test"}
    handler_cls = _make_handler(hook_cfg=hook_cfg, state=state, work_q=work_q, test_once_event=None)
    server = ReusableThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/healthz").json() == {"status": "ok"}

        body = json.dumps(
            {
                "event": "message.created",
                "board": "fresh-board",
                "principal": "qa-bot",
                "subscription_id": "sub-qa",
                "latest_seq": 3,
                "unread_for_you": 1,
                "types": ["task"],
                "mentioned": True,
                "nudge_id": "ndg-3",
            }
        ).encode("utf-8")
        ts = int(time.time())
        sig = sign_nudge("whsec-test", ts, body)

        bad_auth = httpx.post(
            f"{base}/webhook",
            content=body,
            headers={"Authorization": "Bearer wrong", "X-Board-Timestamp": str(ts), "X-Board-Signature": sig},
        )
        assert bad_auth.status_code == 401

        bad_sig = httpx.post(
            f"{base}/webhook",
            content=body,
            headers={
                "Authorization": "Bearer secret-bearer",
                "X-Board-Timestamp": str(ts),
                "X-Board-Signature": "sha256=deadbeef",
            },
        )
        assert bad_sig.status_code == 401

        ok = httpx.post(
            f"{base}/webhook",
            content=body,
            headers={
                "Authorization": "Bearer secret-bearer",
                "X-Board-Timestamp": str(ts),
                "X-Board-Signature": sig,
            },
        )
        assert ok.status_code == 200
        queued = work_q.get(timeout=2.0)
        assert queued is not None
        assert queued["board"] == "fresh-board"
        assert queued["principal"] == "qa-bot"
        assert queued["source"] == "webhook"
    finally:
        server.shutdown()
        server.server_close()


def test_sidecar_binary_resolution_uses_env_and_home_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "board-qa-bot"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")

    prof_dir = tmp_path / ".config" / "agent-board" / "profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)
    (prof_dir / "qa-bot.toml").write_text('url = "http://127.0.0.1:8790"\ntoken = "ab_test"\n', encoding="utf-8")

    monkeypatch.setenv("AGENT_BOARD_BIN", str(bin_dir / "board"))
    monkeypatch.setenv("AGENT_BOARD_AGENTAPI_BIN", "/custom/bin/agentapi")

    assert _resolve_agentapi() == "/custom/bin/agentapi"
    assert _resolve_board_cli("qa-bot") == str(wrapper)

    # Without agent-board-qa-bot in mcp_config.json, prompt uses CLI-first instructions.
    cli_prompt = _build_prompt(
        board="alpha",
        handle="qa-bot",
        latest_seq=9,
        unread_count=2,
        types=["task"],
        mentioned=True,
        source="webhook",
        trigger_seqs=[108, 110],
        trigger_threads=["website-docs"],
        urgent=True,
    )
    assert cli_prompt.startswith("[URGENT / P0] ")
    assert "agent-board-qa-bot" not in cli_prompt
    assert str(wrapper) in cli_prompt
    assert "Triggered by #108, #110 in [website-docs]" in cli_prompt

    # When agent-board-qa-bot IS registered in mcp_config.json, prompt includes both MCP and CLI.
    gem_dir = tmp_path / ".gemini" / "config"
    gem_dir.mkdir(parents=True, exist_ok=True)
    (gem_dir / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"agent-board-qa-bot": {"command": "board"}}}),
        encoding="utf-8",
    )
    mcp_prompt = _build_prompt(
        board="alpha",
        handle="qa-bot",
        latest_seq=9,
        unread_count=2,
        types=["task"],
        mentioned=True,
        source="webhook",
    )
    assert "agent-board-qa-bot" in mcp_prompt
    assert str(wrapper) in mcp_prompt
