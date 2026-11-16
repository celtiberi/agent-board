"""FastAPI + MCP + Web UI ASGI application assembly."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_board.backup.manager import run_backup_scheduler
from agent_board.config import load_settings
from agent_board.errors import DomainError, RateLimitError
from agent_board.http import routes_boards, routes_messages, routes_records
from agent_board.mcp.tools import MCPDispatchMiddleware, build_mcp_server
from agent_board.nudger.worker import NudgeWorker
from agent_board.services.context import AppServices


def create_app(
    services: AppServices | None = None,
    *,
    enable_background_tasks: bool = True,
) -> MCPDispatchMiddleware:
    """Build the full Agent Board ASGI application (REST + MCP + optional Web UI)."""
    own_services = services is None
    app_services = services or AppServices.create(load_settings())
    if app_services.nudge_worker is None:
        app_services.nudge_worker = NudgeWorker(app_services)

    mcp_server, mcp_asgi = build_mcp_server(app_services)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop_event = asyncio.Event()
        bg_tasks: list[asyncio.Task[Any]] = []
        async with mcp_server.session_manager.run():
            if enable_background_tasks:
                bg_tasks.append(asyncio.create_task(app_services.nudge_worker.run_forever(stop_event)))
                bg_tasks.append(asyncio.create_task(run_backup_scheduler(app_services, stop_event)))
            try:
                yield
            finally:
                stop_event.set()
                for task in bg_tasks:
                    task.cancel()
                if bg_tasks:
                    await asyncio.gather(*bg_tasks, return_exceptions=True)
                if own_services:
                    app_services.close()

    api = FastAPI(title="Agent Board", version="0.1.0", lifespan=lifespan)
    api.state.services = app_services

    @api.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> Any:
        headers = {}
        if isinstance(exc, RateLimitError):
            headers["Retry-After"] = str(exc.retry_after)
        accept = request.headers.get("accept", "")
        if not request.url.path.startswith("/api/") and "text/html" in accept:
            from agent_board.web.routes import _TEMPLATES, _session_ctx

            return _TEMPLATES.TemplateResponse(
                request,
                "error.html",
                context={
                    "ctx": _session_ctx(request),
                    "status_code": exc.status_code,
                    "code": exc.code,
                    "message": exc.message,
                },
                status_code=exc.status_code,
                headers=headers,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )

    @api.exception_handler(RequestValidationError)
    async def request_validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": str(exc.errors()[0]["msg"]) if exc.errors() else "Validation error",
                    "details": exc.errors(),
                }
            },
        )

    @api.get("/healthz")
    @api.get("/api/v1/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    api.include_router(routes_boards.router, prefix="/api/v1")
    api.include_router(routes_messages.router, prefix="/api/v1")
    api.include_router(routes_records.router, prefix="/api/v1")

    # Mount Web UI routes if module exists (Phase 3)
    try:
        from agent_board.web.routes import create_web_router

        api.include_router(create_web_router())
    except ImportError:
        pass

    return MCPDispatchMiddleware(api, services=app_services, mcp=mcp_server, mcp_asgi=mcp_asgi)
