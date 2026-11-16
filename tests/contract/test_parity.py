"""Canonical-source parity and Web UI security/CSRF/XSS contract tests."""

from __future__ import annotations

import json
import re

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_board.app import create_app
from agent_board.services import decisions as decisions_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services.auth import set_human_password
from tests.conftest import BoardTestEnv


async def test_web_ui_xss_csrf_and_three_way_parity(env: BoardTestEnv) -> None:
    set_human_password(env.services, handle="owner", password="owner-ui-password-123")

    # Seed messages (including XSS attempt), decisions, and issues
    m1 = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        thread_title="xss-check",
        body_md="Safe text <script>window.pwned=1</script> and [link](https://example.com)",
    )
    decisions_svc.record_decision(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title="Canonical numbers",
        statement="REST, MCP, and Web UI always show identical counts.",
        decided_by="owner",
        decided_on="2026-11-16",
        tags=["parity"],
    )
    issues_svc.open_issue(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        title="Parity issue",
        body_md="Check open issue parity across surfaces.",
        severity="high",
        assignee="mac-dev-ai",
    )

    app = create_app(env.services, enable_background_tasks=False)
    async with app.app.router.lifespan_context(app.app):
        # 1. REST values (via owner token)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {env.owner_token}"},
        ) as rest_client:
            rest_boards = (await rest_client.get("/api/v1/boards")).json()["boards"]
            rest_alpha = next(b for b in rest_boards if b["slug"] == "alpha")

        # 2. MCP values (via owner token)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {env.owner_token}"},
        ) as mcp_hc:
            async with streamable_http_client("http://testserver/mcp", http_client=mcp_hc) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    mcp_boards = json.loads((await session.call_tool("list_boards", {})).content[0].text)["boards"]
                    mcp_alpha = next(b for b in mcp_boards if b["slug"] == "alpha")
                    mcp_brief = json.loads(
                        (await session.call_tool("get_brief", {"board": "alpha", "format": "json"})).content[0].text
                    )

        assert rest_alpha["unread_count"] == mcp_alpha["unread_count"] == mcp_brief["cursor"]["unread_count"]
        assert (
            rest_alpha["active_decisions_count"]
            == mcp_alpha["active_decisions_count"]
            == len(mcp_brief["active_decisions"])
        )
        assert rest_alpha["open_issues_count"] == mcp_alpha["open_issues_count"] == len(mcp_brief["open_issues"])

        # 3. Web UI (logged in as owner via cookie)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as web:
            unauth = await web.get("/")
            assert unauth.status_code == 303
            assert unauth.headers["location"] == "/login"

            login_resp = await web.post(
                "/login",
                data={"handle": "owner", "password": "owner-ui-password-123"},
            )
            assert login_resp.status_code == 303

            home = await web.get("/")
            assert home.status_code == 200
            assert f"{rest_alpha['unread_count']} unread" in home.text
            assert f"Open issues: <strong>{rest_alpha['open_issues_count']}</strong>" in home.text
            assert f"Active decisions: <strong>{rest_alpha['active_decisions_count']}</strong>" in home.text

            board_page = await web.get("/b/alpha")
            assert board_page.status_code == 200
            assert f"Decisions ({rest_alpha['active_decisions_count']})" in board_page.text
            assert f"Issues ({rest_alpha['open_issues_count']})" in board_page.text

            # Thread page sanitizes <script> and adds rel="noopener nofollow"
            thread_page = await web.get(f"/b/alpha/t/{m1['thread_id']}")
            assert thread_page.status_code == 200
            assert "<script>" not in thread_page.text
            assert 'rel="noopener nofollow"' in thread_page.text

            # Decisions, Issues, Issue detail, Search, Admin render cleanly
            decs_page = await web.get("/b/alpha/decisions")
            assert decs_page.status_code == 200 and "D-1" in decs_page.text
            issues_page = await web.get("/b/alpha/issues")
            assert issues_page.status_code == 200 and "I-1" in issues_page.text
            issue_detail = await web.get("/b/alpha/issues/I-1")
            assert issue_detail.status_code == 200 and "Parity issue" in issue_detail.text
            search_page = await web.get("/search?q=Safe")
            assert search_page.status_code == 200 and "<script>" not in search_page.text
            admin_page = await web.get("/admin")
            assert admin_page.status_code == 200 and "Server Admin" in admin_page.text

            # CSRF token missing on POST is rejected with 403
            bad_post = await web.post(
                "/b/alpha/post",
                data={"csrf_token": "wrong-token", "thread_title": "general", "type": "note", "body_md": "hi"},
            )
            assert bad_post.status_code == 403

            # Valid CSRF POST works
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', board_page.text)
            assert m is not None
            csrf_tok = m.group(1)
            ok_post = await web.post(
                "/b/alpha/post",
                data={"csrf_token": csrf_tok, "thread_title": "general", "type": "note", "body_md": "From UI"},
            )
            assert ok_post.status_code == 303
