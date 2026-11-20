from __future__ import annotations

from fastapi.testclient import TestClient

from agent_board import __version__
from agent_board.app import create_app
from agent_board.services import boards as boards_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import messages as messages_svc
from agent_board.store import delivery as delivery_store


def test_patch_subscription_updates_in_place_and_enforces_ownership(env) -> None:
    app = create_app(env.services, enable_background_tasks=False)
    client = TestClient(app)

    # /api/v1 reports package version rather than stale hardcoded 0.1.0.
    index_resp = client.get("/api/v1")
    assert index_resp.status_code == 200
    assert index_resp.json()["version"] == __version__

    # Bob creates a subscription for his own notifications.
    create_resp = client.post(
        "/api/v1/subscriptions",
        headers={"Authorization": f"Bearer {env.bob_token}"},
        json={
            "url": "https://hooks.example.test/bob",
            "boards": ["alpha"],
            "filter": {"exclude_self": True, "mentions_only": False, "types": ["task"]},
            "auth_header": "Bearer initial-secret",
            "min_interval_s": 45,
            "debounce_s": 5,
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    sub_id = created["id"]
    assert created["boards"] == ["alpha"]
    assert created["has_auth_header"] is True

    # Other non-owner agent (mac-dev-ai) cannot patch Bob's subscription.
    forbidden = client.patch(
        f"/api/v1/subscriptions/{sub_id}",
        headers={"Authorization": f"Bearer {env.dev_token}"},
        json={"boards": ["*"]},
    )
    assert forbidden.status_code == 403

    # Bob updates boards, filter, intervals, and clears auth_header in place without changing ID.
    patch_resp = client.patch(
        f"/api/v1/subscriptions/{sub_id}",
        headers={"Authorization": f"Bearer {env.bob_token}"},
        json={
            "boards": ["*"],
            "filter": {"mentions_only": True, "types": None},
            "clear_auth_header": True,
            "min_interval_s": 15,
            "debounce_s": 2,
        },
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["id"] == sub_id
    assert patched["boards"] == ["*"]
    assert patched["filter"] == {"exclude_self": True, "mentions_only": True}
    assert patched["has_auth_header"] is False
    assert patched["min_interval_s"] == 15
    assert patched["debounce_s"] == 2

    # Server owner can update another agent's subscription auth header (with Authorization: prefix normalization).
    owner_patch = client.patch(
        f"/api/v1/subscriptions/{sub_id}",
        headers={"Authorization": f"Bearer {env.owner_token}"},
        json={"auth_header": "Authorization: Bearer rotated-key"},
    )
    assert owner_patch.status_code == 200
    assert owner_patch.json()["has_auth_header"] is True
    listed = delivery_svc.list_subscriptions(env.services, env.owner_ctx, all_principals=True)
    assert len(listed) == 1 and listed[0]["id"] == sub_id


def test_board_cursor_and_members_health_endpoints(env) -> None:
    app = create_app(env.services, enable_background_tasks=False)
    client = TestClient(app)

    idx = client.get("/api/v1")
    assert idx.status_code == 200
    endpoints = idx.json()["endpoints"]
    for key in ("board_brief", "board_messages", "board_cursor", "board_members", "board_ack"):
        assert key in endpoints

    boards_svc.create_board(
        env.services, env.owner_ctx, slug="other-board", name="Other Board", purpose="Other scope."
    )
    boards_svc.create_board(
        env.services, env.owner_ctx, slug="secret-proj", name="Secret Proj", purpose="Owner-only."
    )
    messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="@bob hello alpha",
    )

    cur_before = client.get("/api/v1/boards/alpha/cursor", headers={"Authorization": f"Bearer {env.bob_token}"})
    assert cur_before.status_code == 200
    assert cur_before.json() == {
        "board": "alpha",
        "read_seq": 0,
        "latest_seq": 1,
        "unread_count": 1,
        "updated_at": None,
    }

    ack_resp = client.post(
        "/api/v1/boards/alpha/ack",
        headers={"Authorization": f"Bearer {env.bob_token}"},
        json={"read_seq": 1},
    )
    assert ack_resp.status_code == 200

    cur_after = client.get("/api/v1/boards/alpha/cursor", headers={"Authorization": f"Bearer {env.bob_token}"})
    assert cur_after.status_code == 200
    assert cur_after.json()["read_seq"] == 1
    assert cur_after.json()["unread_count"] == 0
    assert cur_after.json()["updated_at"] is not None

    # Create subscriptions covering: mac-dev-ai (*), bob disabled on alpha, and bob on unrelated board.
    dev_sub = delivery_svc.create_subscription(
        env.services,
        env.owner_ctx,
        url="https://hooks.example.test/dev-secret",
        auth_header="Bearer top-secret-dev-key",
        boards=["*"],
        principal_handle="mac-dev-ai",
    )
    bob_alpha_sub = delivery_svc.create_subscription(
        env.services,
        env.owner_ctx,
        url="https://hooks.example.test/bob-alpha-secret",
        boards=["alpha"],
        principal_handle="bob",
    )
    bob_other_sub = delivery_svc.create_subscription(
        env.services,
        env.owner_ctx,
        url="https://hooks.example.test/bob-other-secret",
        boards=["other-board"],
        principal_handle="bob",
    )
    with env.services.db.write_tx() as conn:
        conn.execute(
            "UPDATE subscriptions SET last_sent_at = ? WHERE id = ?",
            ("2026-11-20T15:12:52Z", dev_sub["id"]),
        )
        delivery_store.set_subscription_active(
            conn,
            sub_id=bob_alpha_sub["id"],
            active=False,
            disabled_reason="too_many_failures",
            updated_at="2026-11-20T15:00:00Z",
        )
        conn.execute("UPDATE subscriptions SET failure_count = 3 WHERE id = ?", (bob_alpha_sub["id"],))

    members_resp = client.get("/api/v1/boards/alpha/members", headers={"Authorization": f"Bearer {env.bob_token}"})
    assert members_resp.status_code == 200
    payload = members_resp.json()
    assert payload["board"] == "alpha"
    assert payload["latest_seq"] == 1
    raw_text = members_resp.text
    for sensitive in (
        "hooks.example.test",
        "top-secret-dev-key",
        dev_sub["id"],
        bob_alpha_sub["id"],
        bob_other_sub["id"],
    ):
        assert sensitive not in raw_text

    by_handle = {m["handle"]: m for m in payload["members"]}
    assert by_handle["bob"]["read_seq"] == 1
    assert by_handle["bob"]["cursor_updated_at"] is not None
    assert by_handle["bob"]["webhook"] == {
        "subscription_count": 1,
        "active_count": 0,
        "disabled_count": 1,
        "failure_count": 3,
        "disabled_reasons": ["too_many_failures"],
        "last_sent_at": None,
    }
    assert by_handle["mac-dev-ai"]["read_seq"] == 0
    assert by_handle["mac-dev-ai"]["webhook"] == {
        "subscription_count": 1,
        "active_count": 1,
        "disabled_count": 0,
        "failure_count": 0,
        "disabled_reasons": [],
        "last_sent_at": "2026-11-20T15:12:52Z",
    }

    forbidden = client.get(
        "/api/v1/boards/secret-proj/members",
        headers={"Authorization": f"Bearer {env.bob_token}"},
    )
    assert forbidden.status_code == 403
