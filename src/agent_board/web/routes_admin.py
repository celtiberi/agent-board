"""Web UI `/admin` routes for server-owner management."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from agent_board.backup import manager as backup_mgr
from agent_board.constants import ACTIONABLE_MESSAGE_TYPES
from agent_board.services import auth as auth_svc
from agent_board.services import boards as boards_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import onboarding as onboarding_svc
from agent_board.services import shout as shout_svc
from agent_board.store import boards as boards_store
from agent_board.web.common import (
    _ONE_TIME_FLASH,
    _redirect,
    _render,
    _require_csrf,
    _services,
    _session_ctx,
)


def register_admin_routes(router: APIRouter) -> None:
    """Attach `/admin` Web UI handlers onto the shared Web UI router."""

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
        res = shout_svc.shout(
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
                "types": list(ACTIONABLE_MESSAGE_TYPES),
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
