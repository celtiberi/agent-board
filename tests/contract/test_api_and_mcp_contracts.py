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
