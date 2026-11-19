from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from agent_board.admin_bot import (
    AdminBotClient,
    AdminBotConfig,
    AdminBotProcessor,
    OllamaIntentParser,
    _make_webhook_handler,
    _ReusableHTTPServer,
    load_admin_bot_config,
    parse_deterministic_commands,
    run_once,
)
from agent_board.app import create_app
from agent_board.nudger.signing import sign_nudge
from agent_board.services import boards as boards_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import messages as messages_svc
from agent_board.services.auth import create_principal, create_token, set_membership, set_principal_tags
from agent_board.store import delivery as delivery_store


def _setup_bot(env: Any, tmp_path: Path, *, ollama: OllamaIntentParser | None = None) -> tuple[
    TestClient,
    AdminBotConfig,
    AdminBotProcessor,
    AdminBotClient,
    Any,
]:
    create_principal(
        env.services,
        env.owner_ctx,
        handle="board-admin",
        kind="agent",
        display_name="Board Admin",
        is_server_owner=True,
    )
    bot_tok = create_token(
        env.services, env.owner_ctx, principal_handle="board-admin", name="bot", board_scope=["*"], max_role="owner"
    )["token"]

    # Create a trusted dev agent, an unrelated private board, and verify non-privileged bob (qa-like).
    create_principal(env.services, env.owner_ctx, handle="dev-agent", kind="agent", display_name="Dev", tags=["dev"])
    boards_svc.create_board(
        env.services, env.owner_ctx, slug="secret-proj", name="Secret Project", purpose="Owner-only board."
    )
    set_principal_tags(env.services, env.owner_ctx, handle="bob", tags=["qa"])

    app = create_app(env.services, enable_background_tasks=False)
    http_client = TestClient(app, base_url="http://testserver/api/v1")
    http_client.headers.update({"Authorization": f"Bearer {bot_tok}"})

    cfg_path = tmp_path / "bot.toml"
    cfg_path.write_text(
        f'board_url = "http://test"\nboard_token = "{bot_tok}"\nhandle = "board-admin"\n'
        'webhook_bind = "127.0.0.1:0"\nwebhook_bearer = "wb_secret"\n'
        'webhook_hmac_secret = "wh_hmac"\nollama_url = ""\nallowed_tags = ["dev"]\n',
        encoding="utf-8",
    )
    cfg = load_admin_bot_config(cfg_path)
    bot_client = AdminBotClient(cfg.board_url, cfg.board_token, client=http_client)
    proc = AdminBotProcessor(cfg, bot_client, ollama)
    from agent_board.services.auth import authenticate_bearer

    return http_client, cfg, proc, bot_client, authenticate_bearer(env.services, bot_tok)


def test_deterministic_command_parser_grammar_and_hard_refusals() -> None:
    assert parse_deterministic_commands("@board-admin help", bot_handle="board-admin") == [{"action": "help"}]
    assert parse_deterministic_commands("@board-admin status", bot_handle="board-admin") == [{"action": "status"}]
    assert parse_deterministic_commands("list boards") == [{"action": "list_boards"}]
    assert parse_deterministic_commands("list members alpha") == [{"action": "list_members", "board_slug": "alpha"}]
    assert parse_deterministic_commands("webhooks") == [{"action": "list_webhooks"}]
    assert parse_deterministic_commands("heal webhooks") == [{"action": "enable_webhooks"}]
    assert parse_deterministic_commands("enable webhook 01ABC") == [
        {"action": "enable_webhooks", "subscription_id": "01ABC"}
    ]
    assert parse_deterministic_commands("run backup") == [{"action": "run_backup"}]
    assert parse_deterministic_commands("add @bob to alpha readonly") == [
        {"action": "set_membership", "target_handle": "bob", "board_slug": "alpha", "role": "readonly"}
    ]
    assert parse_deterministic_commands("set-membership alpha @bob") == [
        {"action": "set_membership", "board_slug": "alpha", "target_handle": "bob", "role": "agent"}
    ]
    assert parse_deterministic_commands("remove @bob from alpha") == [
        {"action": "remove_membership", "target_handle": "bob", "board_slug": "alpha"}
    ]
    assert parse_deterministic_commands("set-tags @bob qa,research") == [
        {"action": "set_tags", "target_handle": "bob", "tags": ["qa", "research"]}
    ]
    assert parse_deterministic_commands("clear-tags @bob") == [
        {"action": "set_tags", "target_handle": "bob", "tags": []}
    ]
    assert parse_deterministic_commands('rename @bob --new-handle bob2 --name "Bob Two"') == [
        {"action": "rename_principal", "target_handle": "bob", "new_handle": "bob2", "name": "Bob Two"}
    ]
    assert parse_deterministic_commands("rename @bob to @bob2") == [
        {"action": "rename_principal", "target_handle": "bob", "new_handle": "bob2", "name": ""}
    ]
    assert parse_deterministic_commands(
        'create board proj-x --name "Proj X" --purpose "Track proj X" --add @bob,@dev-agent'
    ) == [
        {
            "action": "create_board",
            "board_slug": "proj-x",
            "name": "Proj X",
            "purpose": "Track proj X",
            "members": ["bob", "dev-agent"],
        }
    ]

    # Forbidden requests refuse deterministically with zero LLM call.
    assert parse_deterministic_commands("add @bob to alpha owner")[0]["action"] == "refuse"
    assert parse_deterministic_commands("create token for @bob")[0]["action"] == "refuse"
    assert parse_deterministic_commands("create principal @eve")[0]["action"] == "refuse"
    assert parse_deterministic_commands("register webhook https://evil.test")[0]["action"] == "refuse"
    assert parse_deterministic_commands("edit mcp_config.json")[0]["action"] == "refuse"


def test_admin_bot_owner_allowlisted_flows_and_anti_loop(env, tmp_path: Path) -> None:
    _, cfg, proc, bot_client, _ = _setup_bot(env, tmp_path)

    # Create a disabled subscription to verify heal/enable.
    sub = delivery_svc.create_subscription(
        env.services,
        env.owner_ctx,
        url="https://hooks.example.test/bob",
        boards=["*"],
        principal_handle="bob",
    )
    with env.services.db.write_tx() as conn:
        delivery_store.set_subscription_active(
            conn, sub_id=sub["id"], active=False, disabled_reason="too_many_failures", updated_at="2026-11-19T00:00:00Z"
        )

    # Broadcast @all does NOT trigger the bot (even though ops/alpha delivery sees bot as owner),
    # and mentions of unrelated handles get neutralized in replies.
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="@all routine broadcast, no bot action needed",
    )
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md='@board-admin create board gamma --name "Gamma" --purpose "Gamma board" --add @bob,@dev-agent',
    )
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md="@board-admin enable webhooks",
    )
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md="@board-admin set-tags @bob qa,verifier",
    )
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md='@board-admin rename @bob --new-handle bobby --name "Bobby"',
    )
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md="@board-admin remove @bobby from gamma",
    )
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md="@board-admin run backup",
    )

    handled = run_once(cfg, board_client=bot_client)
    assert handled == 6  # @all message was skipped, all 6 direct @board-admin commands executed

    # Cursor advanced past latest message.
    assert bot_client.read_messages("alpha")["messages"] == []

    # Second run handles 0 messages (no self-loop).
    assert run_once(cfg, board_client=bot_client) == 0

    # Side effects landed.
    boards = {b["slug"] for b in bot_client.list_boards()}
    assert "gamma" in boards
    principals = {p["handle"]: p for p in bot_client.list_principals()}
    assert "bobby" in principals and "bob" not in principals
    assert set(principals["bobby"]["tags"]) == {"qa", "verifier"}
    subs = {s["id"]: s for s in bot_client.list_subscriptions()}
    assert subs[sub["id"]]["active"] is True
    gamma_members = {m["principal_handle"] for m in bot_client.list_memberships() if m["board_slug"] == "gamma"}
    assert "dev-agent" in gamma_members and "bobby" not in gamma_members

    # Replies neutralize @mentions and address requester without waking others.
    msgs = bot_client.read_messages("alpha", since_seq=0)["messages"]
    bot_replies = [m for m in msgs if m["author_handle"] == "board-admin"]
    assert len(bot_replies) == 6
    assert all("@bobby" not in r["body_md"] or r["body_md"].startswith(f"@{env.owner_ctx.handle}") for r in bot_replies)


def test_admin_bot_enforces_trusted_vs_standard_caller_boundaries(env, tmp_path: Path) -> None:
    _, cfg, proc, bot_client, _ = _setup_bot(env, tmp_path)
    from agent_board.services.auth import authenticate_bearer

    dev_tok = create_token(
        env.services, env.owner_ctx, principal_handle="dev-agent", name="dev", board_scope=["*"], max_role="agent"
    )["token"]
    dev_ctx = authenticate_bearer(env.services, dev_tok)
    set_membership(env.services, env.owner_ctx, board_slug="alpha", principal_handle="dev-agent", role="agent")

    # 1. Trusted dev-agent can create a new board (auto-gets access) and list ONLY boards they belong to.
    messages_svc.post_message(
        env.services,
        dev_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md='@board-admin create board dev-proj --name "Dev Proj" --purpose "Dev work"',
    )
    # 2. Trusted dev-agent CANNOT add themselves to unrelated existing secret-proj.
    messages_svc.post_message(
        env.services,
        dev_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md="@board-admin add @dev-agent to secret-proj",
    )
    # 3. Trusted dev-agent CANNOT see secret-proj in list boards, nor run remove/rename/backup.
    messages_svc.post_message(
        env.services, dev_ctx, board_slug="alpha", msg_type="question", body_md="@board-admin list boards"
    )
    messages_svc.post_message(
        env.services, dev_ctx, board_slug="alpha", msg_type="task", body_md="@board-admin remove @bob from alpha"
    )
    # 4. Non-dev bob (qa) CANNOT create boards or mutate memberships.
    messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="task",
        body_md='@board-admin create board bob-proj --name "Bob Proj" --purpose "Nope"',
    )

    assert run_once(cfg, board_client=bot_client) == 5

    all_msgs = bot_client.read_messages("alpha", since_seq=0)["messages"]
    replies = [m["body_md"] for m in all_msgs if m["author_handle"] == "board-admin"]
    assert any("Created board `dev-proj`" in r for r in replies)
    assert any("boards you belong to" in r for r in replies)
    assert any("`alpha`" in r and "`dev-proj`" in r and "secret-proj" not in r for r in replies)
    assert any("requires a server owner" in r for r in replies)
    assert any("requires `dev` tag or server owner" in r for r in replies)

    # Confirm dev-agent IS member of dev-proj, NOT member of secret-proj, and bob-proj was NOT created.
    memberships = bot_client.list_memberships()
    assert any(m["board_slug"] == "dev-proj" and m["principal_handle"] == "dev-agent" for m in memberships)
    assert not any(m["board_slug"] == "secret-proj" and m["principal_handle"] == "dev-agent" for m in memberships)
    assert not any(b["slug"] == "bob-proj" for b in bot_client.list_boards())


def test_admin_bot_ollama_fallback_and_webhook_server(env, tmp_path: Path) -> None:
    seen_ollama_bodies: list[dict[str, Any]] = []

    def _ollama_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:12b"}]})
        if request.url.path == "/api/chat":
            body = json.loads(request.content.decode("utf-8"))
            seen_ollama_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "actions": [
                                    {"action": "list_members", "board_slug": "alpha"},
                                ]
                            }
                        )
                    }
                },
            )
        return httpx.Response(404)

    ollama = OllamaIntentParser("http://ollama.test", "gemma4:12b", transport=httpx.MockTransport(_ollama_handler))
    _, cfg, _, bot_client, _ = _setup_bot(env, tmp_path, ollama=ollama)

    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="question",
        body_md="@board-admin hey, could you tell me who is currently a member of alpha?",
    )
    assert run_once(cfg, board_client=bot_client, ollama_parser=ollama) == 1
    assert len(seen_ollama_bodies) == 1
    assert seen_ollama_bodies[0]["think"] is False
    assert seen_ollama_bodies[0]["format"]["type"] == "object"
    msgs = bot_client.read_messages("alpha", since_seq=0)["messages"]
    assert any("**Members on `alpha`**" in m["body_md"] for m in msgs if m["author_handle"] == "board-admin")

    # Webhook server verifies /healthz, auth, HMAC signature, and enqueues board slug.
    work_q: queue.Queue[str | None] = queue.Queue()
    handler_cls = _make_webhook_handler(cfg, ollama, work_q)
    server = _ReusableHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        health = httpx.get(f"{base}/healthz")
        assert health.status_code == 200
        assert health.json()["ollama_ok"] is True

        raw = json.dumps({"board": "ops", "latest_seq": 4}).encode("utf-8")
        ts = int(time.time())
        sig = sign_nudge("wh_hmac", ts, raw)

        assert (
            httpx.post(
                f"{base}/webhook",
                content=raw,
                headers={"Authorization": "Bearer wrong", "X-Board-Timestamp": str(ts), "X-Board-Signature": sig},
            ).status_code
            == 401
        )
        assert (
            httpx.post(
                f"{base}/webhook",
                content=raw,
                headers={
                    "Authorization": "Bearer wb_secret",
                    "X-Board-Timestamp": str(ts),
                    "X-Board-Signature": "sha256=bad",
                },
            ).status_code
            == 401
        )
        ok = httpx.post(
            f"{base}/webhook",
            content=raw,
            headers={"Authorization": "Bearer wb_secret", "X-Board-Timestamp": str(ts), "X-Board-Signature": sig},
        )
        assert ok.status_code == 200
        assert work_q.get(timeout=2.0) == "ops"
    finally:
        server.shutdown()
        server.server_close()
        ollama.close()


def test_admin_bot_ignores_incidental_and_code_quoted_mentions(env, tmp_path: Path) -> None:
    _, cfg, _, bot_client, _ = _setup_bot(env, tmp_path)

    non_command_bodies = [
        "FYI: former `@admin` -> `@tool-master`. Mention `@board-admin`.",
        "FYI: for board tasks, mention @board-admin.",
        "@bob please ask @board-admin for help",
        "> @board-admin status",
        "```\n@board-admin status\n```",
        "`@board-admin` status",
    ]
    for idx, body in enumerate(non_command_bodies):
        messages_svc.post_message(
            env.services,
            env.owner_ctx,
            board_slug="alpha",
            msg_type="note" if idx == 0 else "question",
            body_md=body,
        )

    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="question",
        body_md="@board-admin status",
    )
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        msg_type="question",
        body_md="@board-admin, list boards",
    )

    assert run_once(cfg, board_client=bot_client) == 2
    msgs = bot_client.read_messages("alpha", since_seq=0)["messages"]
    bot_replies = [m["body_md"] for m in msgs if m["author_handle"] == "board-admin"]
    assert len(bot_replies) == 2
    assert any("**Server Status**" in r for r in bot_replies)
    assert any("**Boards**" in r for r in bot_replies)


def test_admin_bot_reports_ollama_errors_distinctly(env, tmp_path: Path) -> None:
    def _timeout_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:12b"}]})
        raise httpx.ReadTimeout("timed out", request=request)

    ollama = OllamaIntentParser("http://ollama.test", "gemma4:12b", transport=httpx.MockTransport(_timeout_handler))
    try:
        _, cfg, _, bot_client, _ = _setup_bot(env, tmp_path, ollama=ollama)
        messages_svc.post_message(
            env.services,
            env.owner_ctx,
            board_slug="alpha",
            msg_type="question",
            body_md="@board-admin please check webhook health",
        )
        assert run_once(cfg, board_client=bot_client, ollama_parser=ollama) == 1
        msgs = bot_client.read_messages("alpha", since_seq=0)["messages"]
        bot_replies = [m["body_md"] for m in msgs if m["author_handle"] == "board-admin"]
        assert len(bot_replies) == 1
        assert "Local intent parser error" in bot_replies[0]
        assert "timed out" in bot_replies[0]
        assert "Unrecognized command" not in bot_replies[0]
    finally:
        ollama.close()
