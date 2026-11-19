"""Thin Jinja2 Web UI router using the canonical domain services layer."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from agent_board.backup import manager as backup_mgr
from agent_board.errors import DomainError, PermissionDeniedError
from agent_board.services import attachments as att_svc
from agent_board.services import auth as auth_svc
from agent_board.services import boards as boards_svc
from agent_board.services import brief as brief_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services import onboarding as onboarding_svc
from agent_board.services import search as search_svc
from agent_board.services.context import AppServices, AuthContext
from agent_board.store import boards as boards_store
from agent_board.web.rendering import (
    format_chicago_time,
    render_markdown_safe,
    render_search_snippet_safe,
)

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
_TEMPLATES.env.autoescape = True
_TEMPLATES.env.filters["md"] = render_markdown_safe
_TEMPLATES.env.filters["snippet"] = render_search_snippet_safe
_TEMPLATES.env.filters["chicago_time"] = format_chicago_time
_TEMPLATES.env.filters["urlencode"] = lambda v: quote_plus(str(v or ""))

# One-time ephemeral reveal cache keyed by session token hash (only shown once after creation)
_ONE_TIME_FLASH: dict[str, dict[str, Any]] = {}


def _services(request: Request) -> AppServices:
    return request.app.state.services


def _session_ctx(request: Request) -> AuthContext | None:
    cookie = request.cookies.get("ab_session")
    ip = request.client.host if request.client else None
    return auth_svc.authenticate_session(_services(request), cookie, ip=ip)


def _require_csrf(ctx: AuthContext, submitted_token: str | None) -> None:
    if not submitted_token or submitted_token != ctx.csrf_token:
        raise PermissionDeniedError("Invalid or missing CSRF token", code="csrf_invalid")


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _render(
    request: Request,
    template_name: str,
    *,
    ctx: AuthContext | None,
    status_code: int = 200,
    flash_ok: str | None = None,
    flash_error: str | None = None,
    **kwargs: Any,
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        template_name,
        context={
            "ctx": ctx,
            "flash_ok": flash_ok or request.query_params.get("ok"),
            "flash_error": flash_error or request.query_params.get("err"),
            **kwargs,
        },
        status_code=status_code,
    )


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

        msg = messages_svc.post_message(
            services,
            ctx,
            board_slug=slug,
            msg_type=type,
            body_md=body_md,
            thread_id=thread_id.strip() or None,
            thread_title=thread_title.strip() or None,
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

    @router.get("/admin", response_model=None)
    def web_admin(request: Request) -> Response:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        auth_svc.require_server_owner(ctx)
        services = _services(request)
        flash = _ONE_TIME_FLASH.pop(ctx.csrf_token or "", {})
        return _render(
            request,
            "admin.html",
            ctx=ctx,
            principals=auth_svc.list_principals(services, ctx),
            tokens=auth_svc.list_tokens(services, ctx),
            all_boards=[dict(r) for r in boards_store.list_all_boards(services.db.read_conn())],
            subscriptions=delivery_svc.list_subscriptions(services, ctx, all_principals=True),
            backups=backup_mgr.get_backup_status(services),
            audit_entries=auth_svc.list_audit_logs(services, ctx, limit=50),
            new_token=flash.get("new_token"),
            new_sub_secret=flash.get("new_sub_secret"),
            onboard_result=flash.get("onboard_result"),
            shout_result=flash.get("shout_result"),
        )

    @router.post("/admin/boards")
    def web_admin_create_board(
        request: Request,
        csrf_token: str = Form(...),
        slug: str = Form(...),
        name: str = Form(...),
        purpose: str = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        boards_svc.create_board(_services(request), ctx, slug=slug, name=name, purpose=purpose)
        return _redirect("/admin?ok=Board+created")

    @router.post("/admin/principals")
    def web_admin_create_principal(
        request: Request,
        csrf_token: str = Form(...),
        handle: str = Form(...),
        kind: str = Form(...),
        display_name: str = Form(...),
        tags: str = Form(default=""),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]
        auth_svc.create_principal(
            _services(request),
            ctx,
            handle=handle,
            kind=kind,
            display_name=display_name,
            tags=parsed_tags,
        )
        return _redirect("/admin?ok=Principal+created")

    @router.post("/admin/principals/{handle}/tags")
    def web_admin_set_principal_tags(
        request: Request,
        handle: str,
        csrf_token: str = Form(...),
        tags: str = Form(default=""),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]
        auth_svc.set_principal_tags(_services(request), ctx, handle=handle, tags=parsed_tags)
        return _redirect("/admin?ok=Principal+tags+updated")

    @router.post("/admin/shout")
    def web_admin_shout(
        request: Request,
        csrf_token: str = Form(...),
        target_mode: str = Form(default="tag"),
        tag: str = Form(default=""),
        exclude_boards: str = Form(default=""),
        thread_title: str = Form(default="general"),
        wake: str = Form(default=""),
        body_md: str = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        use_all = target_mode == "all"
        clean_tag = None if use_all else tag.strip()
        if not use_all and not clean_tag:
            return _redirect("/admin?err=Provide+a+tag+or+select+All+active+boards")
        excluded = [b.strip() for b in exclude_boards.split(",") if b.strip()]
        res = messages_svc.shout(
            _services(request),
            ctx,
            body_md=body_md,
            tag=clean_tag,
            all_boards=use_all,
            exclude_boards=excluded,
            thread_title=thread_title.strip() or "general",
            wake=bool(wake),
        )
        _ONE_TIME_FLASH[ctx.csrf_token or ""] = {"shout_result": res}
        return _redirect("/admin?ok=Shout+delivered")

    @router.post("/admin/tokens")
    def web_admin_create_token(
        request: Request,
        csrf_token: str = Form(...),
        principal_handle: str = Form(...),
        name: str = Form(...),
        board_scope: str = Form(...),
        max_role: str = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        scope = [s.strip() for s in board_scope.split(",") if s.strip()]
        tok = auth_svc.create_token(
            _services(request),
            ctx,
            principal_handle=principal_handle,
            name=name,
            board_scope=scope,
            max_role=max_role,
        )
        _ONE_TIME_FLASH[ctx.csrf_token or ""] = {"new_token": tok}
        return _redirect("/admin?ok=Token+created")

    @router.post("/admin/tokens/{token_id}/revoke")
    def web_admin_revoke_token(request: Request, token_id: str, csrf_token: str = Form(...)) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        auth_svc.revoke_token(_services(request), ctx, token_id=token_id)
        return _redirect("/admin?ok=Token+revoked")

    @router.post("/admin/memberships")
    def web_admin_set_membership(
        request: Request,
        csrf_token: str = Form(...),
        board_slug: str = Form(...),
        principal_handle: str = Form(...),
        role: str = Form(...),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        auth_svc.set_membership(
            _services(request),
            ctx,
            board_slug=board_slug,
            principal_handle=principal_handle,
            role=role,
        )
        return _redirect("/admin?ok=Membership+updated")

    @router.post("/admin/onboard")
    async def web_admin_onboard(
        request: Request,
        csrf_token: str = Form(...),
        handle: str = Form(...),
        display_name: str = Form(default=""),
        boards: list[str] = Form(default=[]),
        role: str = Form(default="agent"),
        tags: str = Form(default=""),
        webhook_url: str = Form(default=""),
        webhook_auth_header: str = Form(default=""),
        skip_plain_notes: str = Form(default=""),
        rotate_tokens: str = Form(default=""),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        clean_boards = [b.strip() for b in boards if b.strip()]
        if not clean_boards:
            return _redirect("/admin?err=Select+at+least+one+board")
        res = await onboarding_svc.onboard_agent(
            _services(request),
            ctx,
            handle=handle,
            boards=clean_boards,
            display_name=display_name.strip() or None,
            role=role,
            tags=[t.strip() for t in tags.split(",") if t.strip()] if tags.strip() else None,
            strict_scope=bool(rotate_tokens),
            rotate_tokens=bool(rotate_tokens),
            webhook_url=webhook_url.strip() or None,
            webhook_auth_header=webhook_auth_header.strip() or None,
            skip_plain_notes=bool(skip_plain_notes),
            test_webhook=bool(webhook_url.strip()),
        )
        _ONE_TIME_FLASH[ctx.csrf_token or ""] = {"onboard_result": res}
        return _redirect("/admin?ok=Agent+onboarded")

    @router.post("/admin/subscriptions")
    def web_admin_create_sub(
        request: Request,
        csrf_token: str = Form(...),
        principal_handle: str = Form(...),
        url: str = Form(...),
        auth_header: str = Form(default=""),
        boards: str = Form(default="*"),
        skip_plain_notes: str = Form(default=""),
    ) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        board_list = [b.strip() for b in boards.split(",") if b.strip()] or ["*"]
        filter_cfg = (
            {
                "exclude_self": True,
                "mentions_only": False,
                "types": list(onboarding_svc.ACTIONABLE_MESSAGE_TYPES),
            }
            if bool(skip_plain_notes)
            else None
        )
        sub = delivery_svc.create_subscription(
            _services(request),
            ctx,
            url=url,
            boards=board_list,
            filter_cfg=filter_cfg,
            auth_header=auth_header.strip() or None,
            principal_handle=principal_handle,
        )
        _ONE_TIME_FLASH[ctx.csrf_token or ""] = {"new_sub_secret": sub}
        return _redirect("/admin?ok=Subscription+created")

    @router.post("/admin/subscriptions/{sub_id}/test")
    async def web_admin_test_sub(request: Request, sub_id: str, csrf_token: str = Form(...)) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        auth_svc.require_server_owner(ctx)
        services = _services(request)
        worker = services.nudge_worker
        if worker is None:
            from agent_board.nudger.worker import NudgeWorker

            worker = NudgeWorker(services)
        res = await worker.send_test_nudge(sub_id=sub_id)
        msg = "Test+nudge+succeeded" if res["ok"] else f"Test+nudge+failed:+{quote_plus(str(res['error']))}"
        param = "ok" if res["ok"] else "err"
        return _redirect(f"/admin?{param}={msg}")

    @router.post("/admin/subscriptions/{sub_id}/enable")
    def web_admin_enable_sub(request: Request, sub_id: str, csrf_token: str = Form(...)) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        delivery_svc.reenable_subscription(_services(request), ctx, sub_id=sub_id)
        return _redirect("/admin?ok=Subscription+enabled")

    @router.post("/admin/subscriptions/{sub_id}/delete")
    def web_admin_delete_sub(request: Request, sub_id: str, csrf_token: str = Form(...)) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        delivery_svc.delete_subscription(_services(request), ctx, sub_id=sub_id)
        return _redirect("/admin?ok=Subscription+deleted")

    @router.post("/admin/backups/run")
    def web_admin_run_backup(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
        ctx = _session_ctx(request)
        if ctx is None:
            return _redirect("/login")
        _require_csrf(ctx, csrf_token)
        auth_svc.require_server_owner(ctx)
        res = backup_mgr.run_backup(_services(request))
        if res["ok"]:
            return _redirect("/admin?ok=Backup+completed")
        return _redirect(f"/admin?err={quote_plus(str(res['detail']))}")

    return router
