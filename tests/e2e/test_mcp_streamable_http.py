"""End-to-end test covering all 19 MCP tools over stateless Streamable HTTP."""

from __future__ import annotations

import base64
import json

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_board.app import create_app
from tests.conftest import BoardTestEnv


async def test_mcp_requires_bearer_without_redirect_and_all_tools_work(env: BoardTestEnv) -> None:
    app = create_app(env.services, enable_background_tasks=False)

    async with app.app.router.lifespan_context(app.app):
        # Verify unauthenticated /mcp returns 401 directly (no 307 redirect)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as raw_client:
            unauth = await raw_client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
            assert unauth.status_code == 401
            assert unauth.json()["error"]["code"] == "unauthorized"

        # Connect as Bob via official MCP SDK streamable_http_client
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {env.bob_token}"},
        ) as hc:
            async with streamable_http_client("http://testserver/mcp", http_client=hc) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_resp = await session.list_tools()
                    tool_names = {t.name for t in tools_resp.tools}
                    expected_tools = {
                        "list_boards",
                        "get_brief",
                        "list_threads",
                        "get_thread",
                        "read_messages",
                        "get_message",
                        "post_message",
                        "edit_message",
                        "ack",
                        "list_decisions",
                        "record_decision",
                        "supersede_decision",
                        "list_issues",
                        "open_issue",
                        "update_issue",
                        "close_issue",
                        "reopen_issue",
                        "search",
                        "upload_attachment",
                    }
                    assert expected_tools <= tool_names

                    # 1. list_boards
                    res = await session.call_tool("list_boards", {})
                    assert not res.is_error
                    assert json.loads(res.content[0].text)["boards"][0]["slug"] == "alpha"

                    # 2. upload_attachment + post_message
                    up = await session.call_tool(
                        "upload_attachment",
                        {
                            "board": "alpha",
                            "filename": "qa.log",
                            "content_base64": base64.b64encode(b"qa passed\n").decode("ascii"),
                        },
                    )
                    assert not up.is_error
                    att_id = json.loads(up.content[0].text)["id"]

                    posted = await session.call_tool(
                        "post_message",
                        {
                            "board": "alpha",
                            "type": "note",
                            "thread_title": "mcp-e2e",
                            "body_md": "Hello from MCP @mac-dev-ai",
                            "attachment_ids": [att_id],
                            "idempotency_key": "mcp-post-1",
                        },
                    )
                    assert not posted.is_error
                    msg_id = json.loads(posted.content[0].text)["id"]

                    # 3. edit_message + get_message
                    edited = await session.call_tool(
                        "edit_message",
                        {"board": "alpha", "message_id": msg_id, "body_md": "Hello from MCP v2"},
                    )
                    assert not edited.is_error
                    msg_hist = await session.call_tool("get_message", {"board": "alpha", "message_id": msg_id})
                    assert len(json.loads(msg_hist.content[0].text)["versions"]) == 2

                    # 4. list_threads + get_thread
                    threads = await session.call_tool("list_threads", {"board": "alpha"})
                    mcp_thread = next(
                        t for t in json.loads(threads.content[0].text)["threads"] if t["title"] == "mcp-e2e"
                    )
                    snap = await session.call_tool("get_thread", {"board": "alpha", "thread_id": mcp_thread["id"]})
                    assert json.loads(snap.content[0].text)["messages"][0]["body_md"] == "Hello from MCP v2"

                    # 5. record_decision + supersede_decision + list_decisions
                    d1 = await session.call_tool(
                        "record_decision",
                        {
                            "board": "alpha",
                            "title": "MCP First",
                            "statement": "Bob connects via stateless Streamable HTTP MCP.",
                            "decided_by": "owner",
                            "decided_on": "2026-11-16",
                        },
                    )
                    assert json.loads(d1.content[0].text)["ref"] == "D-1"

                    d2 = await session.call_tool(
                        "supersede_decision",
                        {
                            "board": "alpha",
                            "old_id": "D-1",
                            "title": "MCP Stateless HTTP",
                            "statement": "Bob connects via stateless Streamable HTTP MCP with Bearer auth.",
                            "decided_by": "owner",
                            "decided_on": "2026-11-16",
                            "reason": "Clarify auth header",
                        },
                    )
                    assert json.loads(d2.content[0].text)["ref"] == "D-2"

                    decs = await session.call_tool("list_decisions", {"board": "alpha"})
                    assert len(json.loads(decs.content[0].text)["decisions"]) == 1

                    # 6. open_issue + update_issue + close_issue + reopen_issue + list_issues
                    i1 = await session.call_tool(
                        "open_issue",
                        {
                            "board": "alpha",
                            "title": "MCP test issue",
                            "body_md": "Verify full issue cycle over MCP.",
                            "severity": "high",
                            "assignee": "mac-dev-ai",
                        },
                    )
                    assert json.loads(i1.content[0].text)["ref"] == "I-1"

                    await session.call_tool(
                        "update_issue",
                        {"board": "alpha", "issue_id": "I-1", "status": "in_progress"},
                    )
                    closed = await session.call_tool(
                        "close_issue",
                        {
                            "board": "alpha",
                            "issue_id": "I-1",
                            "resolution": "Resolved cleanly in unit and MCP verification tests.",
                        },
                    )
                    assert json.loads(closed.content[0].text)["status"] == "resolved"

                    reopened = await session.call_tool(
                        "reopen_issue",
                        {"board": "alpha", "issue_id": "I-1", "reason": "Test reopen via MCP"},
                    )
                    assert json.loads(reopened.content[0].text)["status"] == "open"

                    issues = await session.call_tool("list_issues", {"board": "alpha"})
                    assert len(json.loads(issues.content[0].text)["issues"]) == 1

                    # 7. read_messages + ack + get_brief + search
                    stream = await session.call_tool("read_messages", {"board": "alpha"})
                    stream_data = json.loads(stream.content[0].text)
                    assert len(stream_data["messages"]) >= 5

                    acked = await session.call_tool(
                        "ack", {"board": "alpha", "read_seq": stream_data["next_since_seq"]}
                    )
                    assert json.loads(acked.content[0].text)["unread_count"] == 0

                    brief_md = await session.call_tool("get_brief", {"board": "alpha"})
                    assert "# Project Brief:" in brief_md.content[0].text

                    srch = await session.call_tool("search", {"q": "stateless", "board": "alpha"})
                    assert json.loads(srch.content[0].text)["total"] >= 1
