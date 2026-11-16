"""FastAPI dependency helpers for auth, rate limiting, and service access."""

from __future__ import annotations

from fastapi import Header, Request

from agent_board.errors import AuthError, PermissionDeniedError
from agent_board.services.auth import authenticate_bearer, authenticate_session
from agent_board.services.context import AppServices, AuthContext


def get_services(request: Request) -> AppServices:
    return request.app.state.services


def _extract_client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _authenticate_api_request(
    request: Request,
    *,
    is_write: bool,
    authorization: str | None,
    x_csrf_token: str | None,
) -> AuthContext:
    services = get_services(request)
    ip = _extract_client_ip(request)
    ctx: AuthContext | None = None

    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthError("Authorization header must use Bearer <token>")
        ctx = authenticate_bearer(services, token.strip(), ip=ip)
        limiter_key = f"tok:{ctx.token_id}"
    else:
        session_cookie = request.cookies.get("ab_session")
        if session_cookie:
            ctx = authenticate_session(services, session_cookie, ip=ip)
            if ctx is not None:
                if is_write and (not x_csrf_token or x_csrf_token != ctx.csrf_token):
                    raise PermissionDeniedError("Invalid or missing CSRF token", code="csrf_invalid")
                limiter_key = f"sess:{ctx.principal_id}"

    if ctx is None:
        raise AuthError("Bearer token or valid session required")

    services.rate_limiter.check(limiter_key, is_write=is_write)
    return ctx


def require_api_read(
    request: Request,
    authorization: str | None = Header(default=None),
    x_csrf_token: str | None = Header(default=None),
) -> AuthContext:
    return _authenticate_api_request(request, is_write=False, authorization=authorization, x_csrf_token=x_csrf_token)


def require_api_write(
    request: Request,
    authorization: str | None = Header(default=None),
    x_csrf_token: str | None = Header(default=None),
) -> AuthContext:
    return _authenticate_api_request(request, is_write=True, authorization=authorization, x_csrf_token=x_csrf_token)
