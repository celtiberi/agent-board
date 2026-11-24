"""Shared template, session, CSRF, and redirect helpers for the Web UI."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agent_board.errors import PermissionDeniedError
from agent_board.services import auth as auth_svc
from agent_board.services.context import AppServices, AuthContext
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
    services = _services(request)
    ctx = auth_svc.authenticate_session(services, cookie, ip=ip)
    if ctx is not None:
        return ctx
    return auth_svc.authenticate_trusted_web_ip(services, ip=ip)


def _require_csrf(ctx: AuthContext, submitted_token: str | None) -> None:
    if not submitted_token or not ctx.csrf_token or not hmac.compare_digest(submitted_token, ctx.csrf_token):
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
