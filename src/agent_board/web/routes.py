"""Thin Jinja2 Web UI router using the canonical domain services layer."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from agent_board.errors import DomainError
from agent_board.services import attachments as att_svc
from agent_board.services import auth as auth_svc
from agent_board.services import boards as boards_svc
from agent_board.services import brief as brief_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services import monitor as monitor_svc
from agent_board.services import search as search_svc
from agent_board.web.common import (
    _TEMPLATES as _TEMPLATES,
)
from agent_board.web.common import (
    _WEB_DIR,
    _redirect,
    _render,
    _require_csrf,
    _services,
)
from agent_board.web.common import (
    _session_ctx as _session_ctx,
)
from agent_board.web.routes_admin import register_admin_routes


def create_web_router() -> APIRouter:
    router = APIRouter()

    @router.get("/static/style.css")
    def static_css() -> FileResponse:
        return FileResponse(_WEB_DIR / "static" / "style.css", media_type="text/css")

    @router.get("/login", response_model=None)
    def login_page(request: Request) -> Response:
        if _session_ctx(request) is not None:
            return _redirect("/")
        return _render(request, "login.html", ctx=None, handle="", error=None)

    @router.post("/login", response_model=None)
    def login_submit(
        request: Request,
        handle: str = Form(...),
        password: str = Form(...),
        totp_code: str = Form(default=""),
    ) -> Response:
        services = _services(request)
        ip = request.client.host if request.client else None
        try:
            session_tok, _ctx = auth_svc.login_human(
                services,
                handle=handle,
                password=password,
                totp_code=totp_code.strip() or None,
                ip=ip,
            )
        except DomainError as exc:
            return _render(
                request,
                "login.html",
                ctx=None,
                status_code=exc.status_code,
                handle=handle,
                error=exc.message,
            )

        resp = _redirect("/")
        resp.set_cookie(
            key="ab_session",
            value=session_tok,
            httponly=True,
            samesite="strict",
            secure=services.settings.is_https_public,
            max_age=14 * 86400,
            path="/",
        )
        return resp

    @router.post("/logout")
    def logout_submit(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is not None:
            _require_csrf(ctx, csrf_token)
            auth_svc.logout_session(_services(request), request.cookies.get("ab_session"))
        resp = _redirect("/login")
        resp.delete_cookie("ab_session", path="/")
        return resp

    @router.get("/", response_model=None)
    def boards_index(request: Request) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        boards = boards_svc.list_boards(_services(request), ctx)
        return _render(request, "boards.html", ctx=ctx, boards=boards)

    @router.get("/monitor", response_model=None)
    def web_monitor(
        request: Request,
        hours: int = Query(default=24, ge=1, le=168),
        board: str = Query(default=""),
        agent: str = Query(default=""),
        limit: int = Query(default=60, ge=1, le=200),
    ) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        auth_svc.require_server_owner(ctx)
        snapshot = monitor_svc.get_monitor_snapshot(
            _services(request),
            ctx,
            hours=hours,
            board_slug=board or None,
            agent_handle=agent or None,
            limit=limit,
        )
        return _render(request, "monitor.html", ctx=ctx, snapshot=snapshot)

    @router.get("/monitor/data", response_model=None)
    def web_monitor_data(
        request: Request,
        hours: int = Query(default=24, ge=1, le=168),
        board: str = Query(default=""),
        agent: str = Query(default=""),
        limit: int = Query(default=60, ge=1, le=200),
    ) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized", "message": "Login required"}},
            )
        auth_svc.require_server_owner(ctx)
        snapshot = monitor_svc.get_monitor_snapshot(
            _services(request),
            ctx,
            hours=hours,
            board_slug=board or None,
            agent_handle=agent or None,
            limit=limit,
        )
        return JSONResponse(content=snapshot)

    @router.get("/b/{slug}", response_model=None)
    def board_home(request: Request, slug: str, state: str = Query(default="open")) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        services = _services(request)
        board = boards_svc.get_board(services, ctx, board_slug=slug)
        brief = brief_svc.get_brief(services, ctx, board_slug=slug)
        threads = boards_svc.list_threads(services, ctx, board_slug=slug, state=state)
        return _render(
            request,
            "board_home.html",
            ctx=ctx,
            board=board,
            brief=brief,
            threads=threads,
            state=state,
        )

    @router.post("/b/{slug}/ack")
    def web_ack(
        request: Request,
        slug: str,
        csrf_token: str = Form(...),
        read_seq: int = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        delivery_svc.ack_cursor(_services(request), ctx, board_slug=slug, read_seq=read_seq)
        return _redirect(f"/b/{slug}?ok=Cursor+updated")

    @router.post("/b/{slug}/post")
    async def web_post_message(
        request: Request,
        slug: str,
        csrf_token: str = Form(...),
        thread_id: str = Form(default=""),
        thread_title: str = Form(default=""),
        type: str = Form(default="note"),
        target_handle: str = Form(default=""),
        to_handles: str = Form(default=""),
        cc_handles: str = Form(default=""),
        silent: bool = Form(default=False),
        reply_to_id: str = Form(default=""),
        body_md: str = Form(...),
        attachment: UploadFile | None = File(default=None),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        services = _services(request)

        att_ids: list[str] = []
        if attachment is not None and attachment.filename:
            raw = await attachment.read()
            if raw:
                att = att_svc.upload_attachment(
                    services,
                    ctx,
                    board_slug=slug,
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    content_bytes=raw,
                )
                att_ids.append(att["id"])

        data: dict[str, Any] | None = None
        if type == "task" and target_handle.strip():
            data = {"assignee": target_handle.strip().lower(), "state": "open"}
        elif type == "question" and target_handle.strip():
            data = {"for": target_handle.strip().lower()}

        parsed_to: list[str] | None
        if silent:
            parsed_to = []
        elif to_handles.strip():
            parsed_to = [h.strip() for h in to_handles.split(",") if h.strip()]
        else:
            parsed_to = None
        parsed_cc = [h.strip() for h in cc_handles.split(",") if h.strip()] if cc_handles.strip() else None

        msg = messages_svc.post_message(
            services,
            ctx,
            board_slug=slug,
            msg_type=type,
            body_md=body_md,
            thread_id=thread_id.strip() or None,
            thread_title=thread_title.strip() or None,
            to=parsed_to,
            cc=parsed_cc,
            data=data,
            reply_to_id=reply_to_id.strip() or None,
            attachment_ids=att_ids or None,
        )
        return _redirect(f"/b/{slug}/t/{msg['thread_id']}#m-{msg['id']}")

    @router.get("/b/{slug}/t/{thread_id}", response_model=None)
    def thread_view(request: Request, slug: str, thread_id: str) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        services = _services(request)
        board = boards_svc.get_board(services, ctx, board_slug=slug)
        snap = messages_svc.get_thread_snapshot(services, ctx, board_slug=slug, thread_id=thread_id)
        for m in snap["messages"]:
            if m["version_count"] > 1:
                m["versions"] = messages_svc.get_message_with_history(
                    services, ctx, board_slug=slug, message_id=m["id"]
                )["versions"]
            else:
                m["versions"] = []
        return _render(
            request,
            "thread.html",
            ctx=ctx,
            board=board,
            thread=snap["thread"],
            messages=snap["messages"],
        )

    @router.post("/b/{slug}/t/{thread_id}/close")
    def web_close_thread(request: Request, slug: str, thread_id: str, csrf_token: str = Form(...)) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        boards_svc.close_thread(_services(request), ctx, board_slug=slug, thread_id=thread_id, closed=True)
        return _redirect(f"/b/{slug}/t/{thread_id}")

    @router.post("/b/{slug}/t/{thread_id}/reopen")
    def web_reopen_thread(request: Request, slug: str, thread_id: str, csrf_token: str = Form(...)) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        boards_svc.close_thread(_services(request), ctx, board_slug=slug, thread_id=thread_id, closed=False)
        return _redirect(f"/b/{slug}/t/{thread_id}")

    @router.post("/b/{slug}/m/{message_id}/edit")
    def web_edit_message(
        request: Request,
        slug: str,
        message_id: str,
        csrf_token: str = Form(...),
        thread_id: str = Form(...),
        body_md: str = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        messages_svc.edit_message(_services(request), ctx, board_slug=slug, message_id=message_id, body_md=body_md)
        return _redirect(f"/b/{slug}/t/{thread_id}?ok=Message+edited")

    @router.post("/b/{slug}/m/{message_id}/redact")
    def web_redact_message(
        request: Request,
        slug: str,
        message_id: str,
        csrf_token: str = Form(...),
        thread_id: str = Form(...),
        reason: str = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        messages_svc.redact_message(_services(request), ctx, board_slug=slug, message_id=message_id, reason=reason)
        return _redirect(f"/b/{slug}/t/{thread_id}?ok=Message+redacted")

    @router.get("/b/{slug}/decisions", response_model=None)
    def web_decisions(
        request: Request,
        slug: str,
        title: str = Query(default=""),
        statement: str = Query(default=""),
    ) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        services = _services(request)
        board = boards_svc.get_board(services, ctx, board_slug=slug)
        all_decs = decisions_svc.list_decisions(services, ctx, board_slug=slug, status="all")
        active = [d for d in all_decs if d["status"] == "active"]
        historical = [d for d in all_decs if d["status"] != "active"]

        groups: dict[str, list[dict[str, Any]]] = {}
        for d in active:
            primary_tag = d["tags"][0] if d["tags"] else "general"
            groups.setdefault(primary_tag, []).append(d)
        grouped_active = sorted(groups.items(), key=lambda item: item[0])

        return _render(
            request,
            "decisions.html",
            ctx=ctx,
            board=board,
            grouped_active=grouped_active,
            active_count=len(active),
            historical=historical,
            today_date=date.today().isoformat(),
            prefill_title=title,
            prefill_statement=statement,
        )

    @router.get("/b/{slug}/decisions/export.md", response_model=None)
    def web_decisions_export(request: Request, slug: str) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        md = decisions_svc.export_decisions_md(_services(request), ctx, board_slug=slug)
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")

    @router.post("/b/{slug}/decisions")
    def web_record_decision(
        request: Request,
        slug: str,
        csrf_token: str = Form(...),
        title: str = Form(...),
        statement: str = Form(...),
        decided_by: str = Form(...),
        decided_on: str = Form(...),
        rationale: str = Form(default=""),
        tags: str = Form(default=""),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        dec = decisions_svc.record_decision(
            _services(request),
            ctx,
            board_slug=slug,
            title=title,
            statement=statement,
            decided_by=decided_by,
            decided_on=decided_on,
            rationale=rationale or None,
            tags=tag_list,
        )
        return _redirect(f"/b/{slug}/decisions?ok=Recorded+{dec['ref']}")

    @router.post("/b/{slug}/decisions/{ref}/supersede")
    def web_supersede_decision(
        request: Request,
        slug: str,
        ref: str,
        csrf_token: str = Form(...),
        title: str = Form(...),
        statement: str = Form(...),
        decided_by: str = Form(...),
        decided_on: str = Form(...),
        reason: str = Form(...),
        tags: str = Form(default=""),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        new_dec = decisions_svc.supersede_decision(
            _services(request),
            ctx,
            board_slug=slug,
            old_ref=ref,
            title=title,
            statement=statement,
            decided_by=decided_by,
            decided_on=decided_on,
            reason=reason,
            tags=tag_list,
        )
        return _redirect(f"/b/{slug}/decisions?ok=Superseded+{ref}+with+{new_dec['ref']}")

    @router.post("/b/{slug}/decisions/{ref}/revoke")
    def web_revoke_decision(
        request: Request,
        slug: str,
        ref: str,
        csrf_token: str = Form(...),
        reason: str = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        decisions_svc.revoke_decision(_services(request), ctx, board_slug=slug, ref=ref, reason=reason)
        return _redirect(f"/b/{slug}/decisions?ok=Revoked+{ref}")

    @router.get("/b/{slug}/issues", response_model=None)
    def web_issues(
        request: Request,
        slug: str,
        status: str = Query(default="active"),
        severity: str = Query(default=""),
        title: str = Query(default=""),
        body_md: str = Query(default=""),
    ) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        services = _services(request)
        board = boards_svc.get_board(services, ctx, board_slug=slug)
        issues = issues_svc.list_issues(services, ctx, board_slug=slug, status=status, severity=severity or None)
        return _render(
            request,
            "issues.html",
            ctx=ctx,
            board=board,
            issues=issues,
            status=status,
            severity=severity,
            prefill_title=title,
            prefill_body=body_md,
        )

    @router.post("/b/{slug}/issues")
    def web_open_issue(
        request: Request,
        slug: str,
        csrf_token: str = Form(...),
        title: str = Form(...),
        body_md: str = Form(...),
        severity: str = Form(default="normal"),
        assignee: str = Form(default=""),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        iss = issues_svc.open_issue(
            _services(request),
            ctx,
            board_slug=slug,
            title=title,
            body_md=body_md,
            severity=severity,
            assignee=assignee.strip() or None,
        )
        return _redirect(f"/b/{slug}/issues/{iss['ref']}?ok=Opened+{iss['ref']}")

    @router.get("/b/{slug}/issues/{ref}", response_model=None)
    def web_issue_detail(request: Request, slug: str, ref: str) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        services = _services(request)
        board = boards_svc.get_board(services, ctx, board_slug=slug)
        detail = issues_svc.get_issue_detail(services, ctx, board_slug=slug, ref=ref)
        return _render(
            request,
            "issue_detail.html",
            ctx=ctx,
            board=board,
            issue=detail["issue"],
            events=detail["events"],
        )

    @router.post("/b/{slug}/issues/{ref}/update")
    def web_update_issue(
        request: Request,
        slug: str,
        ref: str,
        csrf_token: str = Form(...),
        status: str = Form(...),
        assignee: str = Form(default=""),
        comment: str = Form(default=""),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        issues_svc.update_issue(
            _services(request),
            ctx,
            board_slug=slug,
            ref=ref,
            status=status,
            assignee=assignee.strip(),
            comment=comment.strip() or None,
        )
        return _redirect(f"/b/{slug}/issues/{ref}?ok=Updated+{ref}")

    @router.post("/b/{slug}/issues/{ref}/close")
    def web_close_issue(
        request: Request,
        slug: str,
        ref: str,
        csrf_token: str = Form(...),
        status: str = Form(default="resolved"),
        resolution: str = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        issues_svc.close_issue(_services(request), ctx, board_slug=slug, ref=ref, resolution=resolution, status=status)
        return _redirect(f"/b/{slug}/issues/{ref}?ok=Closed+{ref}")

    @router.post("/b/{slug}/issues/{ref}/reopen")
    def web_reopen_issue(
        request: Request,
        slug: str,
        ref: str,
        csrf_token: str = Form(...),
        reason: str = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        issues_svc.reopen_issue(_services(request), ctx, board_slug=slug, ref=ref, reason=reason)
        return _redirect(f"/b/{slug}/issues/{ref}?ok=Reopened+{ref}")

    @router.get("/search", response_model=None)
    def web_search(
        request: Request,
        q: str = Query(default=""),
        board: str = Query(default=""),
        types: str = Query(default=""),
    ) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        services = _services(request)
        boards = boards_svc.list_boards(services, ctx)
        results = None
        if q.strip():
            results = search_svc.search(
                services,
                ctx,
                query=q,
                board_slug=board.strip() or None,
                types=[types.strip()] if types.strip() else None,
            )
        return _render(
            request,
            "search.html",
            ctx=ctx,
            q=q,
            board_slug=board,
            type_filter=types,
            boards=boards,
            results=results,
        )

    register_admin_routes(router)
    return router
