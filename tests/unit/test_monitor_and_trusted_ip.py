from __future__ import annotations

import os
import re

from fastapi.testclient import TestClient

from agent_board.app import create_app
from agent_board.config import load_settings
from agent_board.services import boards as boards_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.store import delivery as delivery_store


def test_monitor_api_and_non_secret_aggregation(env) -> None:
    app = create_app(env.services, enable_background_tasks=False)
    client = TestClient(app)

    # Create a second active board and seed messages with to/cc/silent envelopes
    boards_svc.create_board(env.services, env.owner_ctx, slug="beta", name="Beta", purpose="Second board")
    bob_sub = delivery_svc.create_subscription(
        env.services,
        env.bob_ctx,
        url="https://hooks.example.test/secret-bob-path",
        boards=["*"],
        auth_header="Bearer top-secret-bob-auth",
        hmac_secret="top-secret-bob-hmac",
        min_interval_s=90,
        debounce_s=15,
    )
    # mac-dev-ai has no webhook (pull-only); viewer is readonly with no webhook
    # Post messages across boards
    messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="task",
        thread_title="sync",
        body_md="Please verify endpoint",
        to=["bob"],
        cc=["viewer"],
    )
    messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        thread_title="sync",
        body_md="Thanks, noted.",
        to=[],
    )
    messages_svc.post_message(
        env.services,
        env.owner_ctx,
        board_slug="beta",
        msg_type="note",
        body_md="Welcome to beta",
        to=[],
    )
    issues_svc.open_issue(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title="Monitor smoke issue",
        body_md="Track monitor health alert.",
        severity="high",
        assignee="mac-dev-ai",
    )

    with env.services.db.write_tx() as conn:
        conn.execute(
            "UPDATE outbox SET last_error = ? WHERE subscription_id = ?",
            ("ConnectError https://hooks.example.test/secret-bob-path timeout", bob_sub["id"]),
        )
        delivery_store.set_subscription_active(
            conn,
            sub_id=bob_sub["id"],
            active=False,
            disabled_reason="Auto-disabled hitting https://hooks.example.test/secret-bob-path",
            updated_at="2026-11-21T12:00:00Z",
        )

    # Non-owner agent is rejected
    denied = client.get("/api/v1/admin/monitor", headers={"Authorization": f"Bearer {env.bob_token}"})
    assert denied.status_code == 403

    # Owner succeeds and receives complete non-secret snapshot
    resp = client.get("/api/v1/admin/monitor?hours=24", headers={"Authorization": f"Bearer {env.owner_token}"})
    assert resp.status_code == 200
    raw = resp.text
    for secret in (
        "hooks.example.test",
        "secret-bob-path",
        "top-secret-bob-auth",
        "top-secret-bob-hmac",
        bob_sub["id"],
    ):
        assert secret not in raw

    data = resp.json()
    assert data["summary"]["active_boards"] == 3
    assert {"ops", "alpha", "beta"} <= {b["slug"] for b in data["boards"]}
    assert data["summary"]["messages_window"] >= 4
    assert data["summary"]["silent_messages_window"] >= 2
    assert data["summary"]["cc_messages_window"] >= 1
    assert data["summary"]["open_issues_total"] == 1
    assert len(data["timeline"]) >= 24

    by_pair = {(r["board_slug"], r["handle"]): r for r in data["agent_matrix"]}
    assert by_pair[("alpha", "bob")]["webhook"]["mode"] == "disabled"
    assert by_pair[("alpha", "bob")]["webhook"]["min_interval_s"] == 90
    assert by_pair[("alpha", "bob")]["webhook"]["debounce_s"] == 15
    assert "[url]" in by_pair[("alpha", "bob")]["webhook"]["disabled_reasons"][0]
    assert by_pair[("alpha", "mac-dev-ai")]["webhook"]["mode"] == "no_webhook"

    interactions = {(i["from_handle"], i["to_handle"]): i for i in data["interactions"]}
    assert interactions[("mac-dev-ai", "bob")]["to_count"] == 1
    assert interactions[("mac-dev-ai", "viewer")]["cc_count"] == 1

    # Board & agent filters narrow feed cleanly
    filtered = client.get(
        "/api/v1/admin/monitor?board=beta&hours=6",
        headers={"Authorization": f"Bearer {env.owner_token}"},
    )
    assert filtered.status_code == 200
    f_data = filtered.json()
    assert f_data["summary"]["active_boards"] == 1
    assert {item["board_slug"] for item in f_data["feed"]} == {"beta"}


def test_trusted_ip_web_auto_login_isolation_and_csrf(env, tmp_path) -> None:
    env.settings.web_auto_login_handle = env.owner_ctx.handle
    env.settings.web_trusted_ips = ("100.64.0.10", "127.0.0.1")
    app = create_app(env.services, enable_background_tasks=False)

    trusted_client = TestClient(app, client=("100.64.0.10", 51234))
    untrusted_client = TestClient(app, client=("100.64.0.99", 51234))

    # Seed a message with both To and Cc recipients so SSR Jinja2 rendering is exercised
    messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="note",
        thread_title="ssr-recipients",
        body_md="SSR recipient format check",
        to=["bob"],
        cc=["viewer"],
    )

    # Trusted IP can load Web UI pages and /monitor/data with no login cookie
    home = trusted_client.get("/")
    assert home.status_code == 200
    assert f"@{env.owner_ctx.handle} (trusted)" in home.text
    assert 'action="/logout"' not in home.text

    monitor_page = trusted_client.get("/monitor")
    assert monitor_page.status_code == 200
    assert "Live Activity &amp; Health Monitor" in monitor_page.text
    assert "@bob" in monitor_page.text
    assert "@viewer" in monitor_page.text

    monitor_json = trusted_client.get("/monitor/data?hours=24")
    assert monitor_json.status_code == 200
    assert monitor_json.json()["summary"]["active_boards"] >= 1

    # Untrusted IP still hits /login redirect and 401 on /monitor/data
    u_home = untrusted_client.get("/", follow_redirects=False)
    assert u_home.status_code == 303
    assert u_home.headers["location"] == "/login"

    u_mon = untrusted_client.get("/monitor", follow_redirects=False)
    assert u_mon.status_code == 303
    assert u_mon.headers["location"] == "/login"

    u_mon_data = untrusted_client.get("/monitor/data")
    assert u_mon_data.status_code == 401

    # Critical security boundary: /api/v1/* MUST still require Bearer token even from trusted IP
    api_unauth = trusted_client.get("/api/v1/boards")
    assert api_unauth.status_code == 401
    api_mon_unauth = trusted_client.get("/api/v1/admin/monitor")
    assert api_mon_unauth.status_code == 401

    # Web UI POST forms on trusted IP still enforce CSRF token
    bad_post = trusted_client.post(
        "/b/alpha/post",
        data={"csrf_token": "wrong", "type": "note", "body_md": "Blocked"},
        headers={"accept": "text/html"},
    )
    assert bad_post.status_code == 403

    board_page = trusted_client.get("/b/alpha")
    assert board_page.status_code == 200
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', board_page.text)
    assert m is not None
    csrf = m.group(1)

    ok_post = trusted_client.post(
        "/b/alpha/post",
        data={"csrf_token": csrf, "type": "note", "body_md": "Posted via trusted Web UI", "silent": "true"},
        follow_redirects=False,
    )
    assert ok_post.status_code == 303
    assert "/b/alpha/t/" in ok_post.headers["location"]

    # Non-human handles are ignored by trusted auto-login
    env.settings.web_auto_login_handle = "bob"
    rejected_agent = trusted_client.get("/", follow_redirects=False)
    assert rejected_agent.status_code == 303
    assert rejected_agent.headers["location"] == "/login"

    # Settings loader parses env vars cleanly
    old_h = os.environ.get("AGENT_BOARD_WEB_AUTO_LOGIN_HANDLE")
    old_ips = os.environ.get("AGENT_BOARD_WEB_TRUSTED_IPS")
    try:
        os.environ["AGENT_BOARD_WEB_AUTO_LOGIN_HANDLE"] = " Owner "
        os.environ["AGENT_BOARD_WEB_TRUSTED_IPS"] = "100.64.0.10, 127.0.0.1"
        loaded = load_settings(data_dir=tmp_path / "cfg-test")
        assert loaded.web_auto_login_handle == "owner"
        assert loaded.web_trusted_ips == ("100.64.0.10", "127.0.0.1")
    finally:
        if old_h is None:
            os.environ.pop("AGENT_BOARD_WEB_AUTO_LOGIN_HANDLE", None)
        else:
            os.environ["AGENT_BOARD_WEB_AUTO_LOGIN_HANDLE"] = old_h
        if old_ips is None:
            os.environ.pop("AGENT_BOARD_WEB_TRUSTED_IPS", None)
        else:
            os.environ["AGENT_BOARD_WEB_TRUSTED_IPS"] = old_ips
