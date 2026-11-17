"""Contract tests validating REST API responses against published schemas/api/*.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import jsonschema

from agent_board.app import create_app
from tests.conftest import BoardTestEnv

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas" / "api"


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMAS_DIR / name).read_text(encoding="utf-8"))


async def test_rest_api_matches_published_json_schemas(env: BoardTestEnv) -> None:
    app = create_app(env.services, enable_background_tasks=False)
    board_schema = _load_schema("board.json")
    thread_schema = _load_schema("thread.json")
    msg_schema = _load_schema("message.json")
    dec_schema = _load_schema("decision.json")
    issue_schema = _load_schema("issue.json")
    brief_schema = _load_schema("brief.json")
    search_schema = _load_schema("search_response.json")
    sub_schema = _load_schema("subscription.json")
    err_schema = _load_schema("error.json")

    bob_headers = {"Authorization": f"Bearer {env.bob_token}"}
    owner_headers = {"Authorization": f"Bearer {env.owner_token}"}

    async with app.app.router.lifespan_context(app.app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            health = await client.get("/healthz")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}

            for idx_path in ("/api/v1", "/api/v1/"):
                idx_resp = await client.get(idx_path)
                assert idx_resp.status_code == 200
                idx_json = idx_resp.json()
                assert idx_json["service"] == "agent-board"
                assert idx_json["api_version"] == "v1"
                assert idx_json["status"] == "ok"
                assert idx_json["endpoints"]["boards"] == "/api/v1/boards"

            me = await client.get("/api/v1/me", headers=bob_headers)
            assert me.status_code == 200
            assert me.json()["principal"]["handle"] == "bob"

            boards_resp = await client.get("/api/v1/boards", headers=bob_headers)
            assert boards_resp.status_code == 200
            for b in boards_resp.json()["boards"]:
                jsonschema.validate(b, board_schema)

            # Upload attachment and post message with Idempotency-Key
            up = await client.post(
                "/api/v1/boards/alpha/attachments",
                headers=bob_headers,
                files={"file": ("report.txt", b"hello attachment", "text/plain")},
            )
            assert up.status_code == 201
            att_id = up.json()["id"]

            post_resp = await client.post(
                "/api/v1/boards/alpha/messages",
                headers={**bob_headers, "Idempotency-Key": "rest-msg-1"},
                json={
                    "thread_title": "contract-thread",
                    "type": "note",
                    "body_md": "Testing contract schema @mac-dev-ai",
                    "attachment_ids": [att_id],
                },
            )
            assert post_resp.status_code == 201
            msg_json = post_resp.json()
            jsonschema.validate(msg_json, msg_schema)

            # Idempotent replay returns identical body
            replay = await client.post(
                "/api/v1/boards/alpha/messages",
                headers={**bob_headers, "Idempotency-Key": "rest-msg-1"},
                json={
                    "thread_title": "contract-thread",
                    "type": "note",
                    "body_md": "Testing contract schema @mac-dev-ai",
                    "attachment_ids": [att_id],
                },
            )
            assert replay.status_code == 201
            assert replay.json()["id"] == msg_json["id"]

            # Conflicting Idempotency-Key returns 409 error schema
            conflict = await client.post(
                "/api/v1/boards/alpha/messages",
                headers={**bob_headers, "Idempotency-Key": "rest-msg-1"},
                json={"thread_title": "contract-thread", "type": "note", "body_md": "Changed"},
            )
            assert conflict.status_code == 409
            jsonschema.validate(conflict.json(), err_schema)

            # Download attachment enforces Content-Disposition: attachment + nosniff
            dl = await client.get(f"/api/v1/attachments/{att_id}", headers=bob_headers)
            assert dl.status_code == 200
            assert "attachment" in dl.headers.get("content-disposition", "")
            assert dl.headers.get("x-content-type-options") == "nosniff"
            assert dl.content == b"hello attachment"

            # Threads
            th = await client.get("/api/v1/boards/alpha/threads", headers=bob_headers)
            assert th.status_code == 200
            for t in th.json()["threads"]:
                jsonschema.validate(t, thread_schema)

            # Decision + export.md
            dec = await client.post(
                "/api/v1/boards/alpha/decisions",
                headers=bob_headers,
                json={
                    "title": "Contract format",
                    "statement": "All REST contracts validate against JSON Schema.",
                    "decided_by": "owner",
                    "decided_on": "2026-11-16",
                },
            )
            assert dec.status_code == 201
            jsonschema.validate(dec.json(), dec_schema)

            exp = await client.get("/api/v1/boards/alpha/decisions/export.md", headers=bob_headers)
            assert exp.status_code == 200
            assert "<!-- generated from agent-board; do not edit -->" in exp.text

            # Issue
            iss = await client.post(
                "/api/v1/boards/alpha/issues",
                headers=bob_headers,
                json={
                    "title": "Schema check issue",
                    "body_md": "Verify issue JSON schema contract.",
                    "severity": "high",
                    "assignee": "mac-dev-ai",
                },
            )
            assert iss.status_code == 201
            jsonschema.validate(iss.json(), issue_schema)

            # Brief (markdown + json)
            brief_md = await client.get("/api/v1/boards/alpha/brief", headers=bob_headers)
            assert brief_md.status_code == 200
            assert "# Project Brief:" in brief_md.text

            brief_json = await client.get("/api/v1/boards/alpha/brief?format=json", headers=bob_headers)
            assert brief_json.status_code == 200
            jsonschema.validate(brief_json.json(), brief_schema)

            # Search
            srch = await client.get("/api/v1/search?q=Contract", headers=bob_headers)
            assert srch.status_code == 200
            jsonschema.validate(srch.json(), search_schema)

            # Subscriptions
            sub = await client.post(
                "/api/v1/subscriptions",
                headers=bob_headers,
                json={"url": "https://example.invalid/webhook", "boards": ["alpha"]},
            )
            assert sub.status_code == 201
            jsonschema.validate(sub.json(), sub_schema)

            # Unauthorized request returns 401 matching error.json
            unauth = await client.get("/api/v1/boards")
            assert unauth.status_code == 401
            jsonschema.validate(unauth.json(), err_schema)

            # Owner admin endpoints
            audit = await client.get("/api/v1/admin/audit", headers=owner_headers)
            assert audit.status_code == 200
            assert len(audit.json()["entries"]) > 0

            # One-step agent onboarding (create + scope + token + filtered webhook) & idempotent rotation
            onboard_1 = await client.post(
                "/api/v1/admin/onboard",
                headers=owner_headers,
                json={
                    "handle": "new-qa",
                    "display_name": "New QA Agent",
                    "boards": ["alpha"],
                    "role": "agent",
                    "webhook_url": "https://example.invalid/new-qa-hook",
                    "webhook_auth_header": "Authorization: Bearer hook-secret",
                    "skip_plain_notes": True,
                    "test_webhook": False,
                },
            )
            assert onboard_1.status_code == 201, onboard_1.text
            o1 = onboard_1.json()
            assert o1["principal_handle"] == "new-qa"
            assert o1["env_var_name"] == "AGENT_BOARD_NEW_QA_TOKEN"
            assert o1["token"]["board_scope"] == ["alpha"]
            assert "note" not in o1["subscription"]["filter"]["types"]
            assert "task" in o1["subscription"]["filter"]["types"]

            # Re-running rotates old token and replaces matching webhook without duplicating subscriptions
            onboard_2 = await client.post(
                "/api/v1/admin/onboard",
                headers=owner_headers,
                json={
                    "handle": "new-qa",
                    "boards": ["alpha"],
                    "role": "readonly",
                    "webhook_url": "https://example.invalid/new-qa-hook",
                    "skip_plain_notes": True,
                    "test_webhook": False,
                },
            )
            assert onboard_2.status_code == 201
            o2 = onboard_2.json()
            assert o1["token"]["prefix"] in o2["revoked_prefixes"]
            assert o2["token"]["max_role"] == "readonly"
            subs_all = (await client.get("/api/v1/subscriptions?all=true", headers=owner_headers)).json()[
                "subscriptions"
            ]
            assert sum(1 for s in subs_all if s["principal_handle"] == "new-qa") == 1


async def test_agent_friendly_rest_aliases_and_membership_token_sync(env: BoardTestEnv) -> None:
    app = create_app(env.services, enable_background_tasks=False)
    bob_headers = {"Authorization": f"Bearer {env.bob_token}"}
    owner_headers = {"Authorization": f"Bearer {env.owner_token}"}

    async with app.app.router.lifespan_context(app.app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            idx = await client.get("/api/v1")
            assert idx.status_code == 200
            assert idx.json()["endpoints"]["openapi"] == "/api/v1/openapi.json"

            for oa_path in ("/api/v1/openapi.json", "/api/openapi.json"):
                oa = await client.get(oa_path)
                assert oa.status_code == 200
                assert "paths" in oa.json()

            # Accept "body" alias, thread title in thread_id, boolean passed, and string issue_ids
            post_resp = await client.post(
                "/api/v1/boards/alpha/messages",
                headers=bob_headers,
                json={
                    "thread_id": "general",
                    "type": "retest_report",
                    "body": "Retest passed via aliases",
                    "data": {
                        "target": "staging",
                        "passed": True,
                        "failed": 0,
                        "issue_ids": "I-1",
                    },
                },
            )
            assert post_resp.status_code == 201, post_resp.text
            posted = post_resp.json()
            assert posted["thread_title"] == "general"
            assert posted["data"]["issue_ids"] == ["I-1"]
            assert posted["data"]["passed"] is True

            # Raise read limit up to 1000
            read_resp = await client.get("/api/v1/boards/alpha/messages?since_seq=0&limit=500", headers=bob_headers)
            assert read_resp.status_code == 200

            # Strip leading '@' in assignee and normalize 'medium' severity alias
            iss_resp = await client.post(
                "/api/v1/boards/alpha/issues",
                headers=bob_headers,
                json={
                    "title": "Alias normalization issue",
                    "body": "Opened with body alias and @bob assignee",
                    "severity": "medium",
                    "assignee": "@bob",
                },
            )
            assert iss_resp.status_code == 201, iss_resp.text
            iss = iss_resp.json()
            assert iss["assignee_handle"] == "bob"
            assert iss["severity"] == "normal"

            # Flexible /ack: {"seq": N}, ?seq=N, and empty body defaulting to latest_seq
            ack_seq = await client.post("/api/v1/boards/alpha/ack", headers=bob_headers, json={"seq": 1})
            assert ack_seq.status_code == 200
            assert ack_seq.json()["read_seq"] == 1

            ack_qs = await client.post("/api/v1/boards/alpha/ack?seq=1", headers=bob_headers)
            assert ack_qs.status_code == 200
            assert ack_qs.json()["read_seq"] == 1

            ack_empty = await client.post("/api/v1/boards/alpha/ack", headers=bob_headers)
            assert ack_empty.status_code == 200
            assert ack_empty.json()["read_seq"] == ack_empty.json()["latest_seq"]

            # Onboard an agent on alpha, then expand/shrink membership and verify token board_scope sync
            onboard_resp = await client.post(
                "/api/v1/admin/onboard",
                headers=owner_headers,
                json={
                    "handle": "sync-qa",
                    "boards": ["alpha"],
                    "role": "agent",
                    "test_webhook": False,
                },
            )
            assert onboard_resp.status_code == 201, onboard_resp.text
            sync_token = onboard_resp.json()["token"]["token"]
            sync_headers = {"Authorization": f"Bearer {sync_token}"}

            beta_resp = await client.post(
                "/api/v1/boards",
                headers=owner_headers,
                json={"slug": "beta", "name": "Beta", "purpose": "Scope sync test"},
            )
            assert beta_resp.status_code == 201

            add_mem = await client.post(
                "/api/v1/admin/memberships",
                headers=owner_headers,
                json={"board_slug": "beta", "principal_handle": "sync-qa", "role": "agent"},
            )
            assert add_mem.status_code == 200

            visible_after_add = {
                b["slug"] for b in (await client.get("/api/v1/boards", headers=sync_headers)).json()["boards"]
            }
            assert visible_after_add == {"alpha", "beta"}

            rm_mem = await client.delete("/api/v1/admin/memberships/beta/sync-qa", headers=owner_headers)
            assert rm_mem.status_code == 200

            visible_after_rm = {
                b["slug"] for b in (await client.get("/api/v1/boards", headers=sync_headers)).json()["boards"]
            }
            assert visible_after_rm == {"alpha"}
