from __future__ import annotations

from fastapi.testclient import TestClient

from agent_board import __version__
from agent_board.app import create_app
from agent_board.services import delivery as delivery_svc


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
