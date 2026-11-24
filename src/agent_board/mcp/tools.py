"""Thin MCP tool definitions and pure-ASGI Bearer authentication wrapper."""

from __future__ import annotations

import base64
import binascii
import contextvars
import json
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_board.errors import DomainError, ValidationError
from agent_board.services import attachments as att_svc
from agent_board.services import boards as boards_svc
from agent_board.services import brief as brief_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services import search as search_svc
from agent_board.services import timers as timers_svc
from agent_board.services.auth import authenticate_bearer
from agent_board.services.context import AppServices, AuthContext

_CURRENT_AUTH_CTX: contextvars.ContextVar[AuthContext | None] = contextvars.ContextVar(
    "agent_board_mcp_auth_ctx", default=None
)


def _require_ctx(*, is_write: bool, services: AppServices) -> AuthContext:
    ctx = _CURRENT_AUTH_CTX.get()
    if ctx is None:
        raise ToolError("unauthorized: Missing MCP authentication context")
    if is_write:
        try:
            services.rate_limiter.check(f"tok:{ctx.token_id}", is_write=True)
        except DomainError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc
    return ctx


def _run_tool(services: AppServices, *, is_write: bool, fn: Callable[[AuthContext], Any]) -> Any:
    ctx = _require_ctx(is_write=is_write, services=services)
    try:
        return fn(ctx)
    except DomainError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc


def build_mcp_server(services: AppServices) -> tuple[MCPServer, ASGIApp]:
    """Create the MCPServer with all 19 thin tools and return (server, streamable_http_asgi)."""
    mcp = MCPServer(
        name="agent-board",
        instructions=(
            "Agent Board multi-project coordination server. Call list_boards or get_brief(board) "
            "first, read_messages(board) to pull unread messages, act, post_message with an "
            "idempotency_key, and ack(board, read_seq) when finished."
        ),
    )

    @mcp.tool(description="List boards visible to the caller with unread counts and open issues.")
    def list_boards(include_archived: bool = False) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: {"boards": boards_svc.list_boards(services, ctx, include_archived=include_archived)},
        )

    @mcp.tool(
        description=(
            "Get the computed Project Brief for a board (purpose, active decisions, open issues, "
            "latest summary, recent activity, caller tasks/questions, and cursor)."
        )
    )
    def get_brief(board: str, format: str = "markdown") -> str | dict[str, Any]:
        res = _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: brief_svc.get_brief(services, ctx, board_slug=board),
        )
        if format == "json":
            return res
        return str(res["markdown"])

    @mcp.tool(description="List threads on a board filtered by state ('open', 'closed', or 'all').")
    def list_threads(board: str, state: str = "open") -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: {"threads": boards_svc.list_threads(services, ctx, board_slug=board, state=state)},
        )

    @mcp.tool(description="Get a thread's messages (snapshot of latest versions or incremental since_seq).")
    def get_thread(board: str, thread_id: str, since_seq: int | None = None) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: messages_svc.get_thread_snapshot(
                services, ctx, board_slug=board, thread_id=thread_id, since_seq=since_seq
            ),
        )

    @mcp.tool(
        description=(
            "Read board message stream after since_seq (defaults to caller's ack cursor), "
            "ordered by seq up to limit<=200."
        )
    )
    def read_messages(
        board: str,
        since_seq: int | None = None,
        thread: str | None = None,
        types: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: messages_svc.read_messages(
                services,
                ctx,
                board_slug=board,
                since_seq=since_seq,
                thread_id=thread,
                types=types,
                limit=limit,
            ),
        )

    @mcp.tool(description="Get one message and its full edit version history.")
    def get_message(board: str, message_id: str) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: messages_svc.get_message_with_history(
                services, ctx, board_slug=board, message_id=message_id
            ),
        )

    @mcp.tool(
        description=(
            "Post a message (note, task, question, status, retest_report, or summary) to a thread. "
            "Pass thread_id or thread_title (defaults to 'general'; use distinct topic threads per workstream). "
            "Routing envelope: pass `to` (list of handles, tags, or 'all') for recipients who should wake and act, "
            "`cc` for FYI visibility without waking, and `to=[]` for courtesy closures ('thanks', 'noted', 'LGTM') "
            "or quiet logs so nobody wakes. Pass `priority='urgent'` only for P0 emergencies that must bypass "
            "recipient Do-Not-Disturb. If `to` is omitted, paragraph-leading `@handle`s wake before falling "
            "back to body mentions or unmentioned actionable types."
        )
    )
    def post_message(
        board: str,
        type: str,
        body_md: str,
        priority: str = "normal",
        thread_id: str | None = None,
        thread_title: str | None = None,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        data: dict[str, Any] | None = None,
        reply_to_id: str | None = None,
        attachment_ids: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: messages_svc.post_message(
                services,
                ctx,
                board_slug=board,
                msg_type=type,
                priority=priority,
                body_md=body_md,
                thread_id=thread_id,
                thread_title=thread_title,
                to=to,
                cc=cc,
                data=data,
                reply_to_id=reply_to_id,
                attachment_ids=attachment_ids,
                idempotency_key=idempotency_key,
            ),
        )

    @mcp.tool(description="Edit an existing message by appending a new superseding version.")
    def edit_message(
        board: str,
        message_id: str,
        body_md: str,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: messages_svc.edit_message(
                services,
                ctx,
                board_slug=board,
                message_id=message_id,
                body_md=body_md,
                to=to,
                cc=cc,
                data=data,
                idempotency_key=idempotency_key,
            ),
        )

    @mcp.tool(description="Advance caller's read cursor on a board up to read_seq.")
    def ack(board: str, read_seq: int, idempotency_key: str | None = None) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: delivery_svc.ack_cursor(
                services, ctx, board_slug=board, read_seq=read_seq, idempotency_key=idempotency_key
            ),
        )

    @mcp.tool(
        description=(
            "Set caller Do-Not-Disturb (`enabled=True`, default 30m, max 240m) to hold/coalesce non-urgent "
            "webhooks during focused work, or clear (`enabled=False`) to resume and expedite deferred wakes. "
            "Urgent (P0) messages still bypass DND."
        )
    )
    def set_dnd(
        enabled: bool = True,
        duration_minutes: int = 30,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: delivery_svc.set_dnd(
                services,
                ctx,
                enabled=enabled,
                duration_minutes=duration_minutes,
                reason=reason,
            ),
        )

    @mcp.tool(description="List decisions on a board (status: 'active', 'superseded', 'revoked', 'all').")
    def list_decisions(board: str, status: str = "active") -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: {"decisions": decisions_svc.list_decisions(services, ctx, board_slug=board, status=status)},
        )

    @mcp.tool(
        description=(
            "Record a new pinned decision (D-n) and corresponding decision message. "
            "Do not include a 'D-n:' prefix in title (the server assigns the next number); "
            "use supersede_decision when clarifying or replacing an existing decision."
        )
    )
    def record_decision(
        board: str,
        title: str,
        statement: str,
        decided_by: str,
        decided_on: str,
        rationale: str | None = None,
        tags: list[str] | None = None,
        thread: str = "decisions",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: decisions_svc.record_decision(
                services,
                ctx,
                board_slug=board,
                title=title,
                statement=statement,
                decided_by=decided_by,
                decided_on=decided_on,
                rationale=rationale,
                tags=tags,
                thread_title=thread,
                idempotency_key=idempotency_key,
            ),
        )

    @mcp.tool(description="Atomically supersede an active decision (e.g. 'D-7') with a new decision.")
    def supersede_decision(
        board: str,
        old_id: str,
        title: str,
        statement: str,
        decided_by: str,
        decided_on: str,
        reason: str,
        rationale: str | None = None,
        tags: list[str] | None = None,
        thread: str = "decisions",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: decisions_svc.supersede_decision(
                services,
                ctx,
                board_slug=board,
                old_ref=old_id,
                title=title,
                statement=statement,
                decided_by=decided_by,
                decided_on=decided_on,
                reason=reason,
                rationale=rationale,
                tags=tags,
                thread_title=thread,
                idempotency_key=idempotency_key,
            ),
        )

    @mcp.tool(
        description=(
            "List tracked issues on a board (status: 'active', 'open', 'in_progress', 'resolved', 'wont_fix', 'all')."
        )
    )
    def list_issues(board: str, status: str = "active", severity: str | None = None) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: {
                "issues": issues_svc.list_issues(services, ctx, board_slug=board, status=status, severity=severity)
            },
        )

    @mcp.tool(description="Open a new tracked issue (I-n) and its discussion thread.")
    def open_issue(
        board: str,
        title: str,
        body_md: str,
        severity: str = "normal",
        assignee: str | None = None,
        tags: list[str] | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: issues_svc.open_issue(
                services,
                ctx,
                board_slug=board,
                title=title,
                body_md=body_md,
                severity=severity,
                assignee=assignee,
                tags=tags,
                thread_id=thread_id,
                idempotency_key=idempotency_key,
            ),
        )

    @mcp.tool(description="Update an open issue's status ('open'|'in_progress'), assignee handle, or comment.")
    def update_issue(
        board: str,
        issue_id: str,
        status: str | None = None,
        assignee: str | None = None,
        comment: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: issues_svc.update_issue(
                services,
                ctx,
                board_slug=board,
                ref=issue_id,
                status=status,
                assignee=assignee,
                comment=comment,
                idempotency_key=idempotency_key,
            ),
        )

    @mcp.tool(description="Close an issue as 'resolved' or 'wont_fix' with a required >=20 char structural resolution.")
    def close_issue(
        board: str,
        issue_id: str,
        resolution: str,
        status: str = "resolved",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: issues_svc.close_issue(
                services,
                ctx,
                board_slug=board,
                ref=issue_id,
                resolution=resolution,
                status=status,
                idempotency_key=idempotency_key,
            ),
        )

    @mcp.tool(description="Reopen a closed issue with a reason.")
    def reopen_issue(
        board: str,
        issue_id: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: issues_svc.reopen_issue(
                services,
                ctx,
                board_slug=board,
                ref=issue_id,
                reason=reason,
                idempotency_key=idempotency_key,
            ),
        )

    @mcp.tool(description="Full-text search across accessible boards (supports phrases, OR, and prefix*).")
    def search(
        q: str,
        board: str | None = None,
        types: list[str] | None = None,
        author: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: search_svc.search(
                services,
                ctx,
                query=q,
                board_slug=board,
                types=types,
                author_handle=author,
                limit=limit,
            ),
        )

    @mcp.tool(description="Upload an attachment (<= 5 MB base64) to obtain an attachment_id for post_message.")
    def upload_attachment(
        board: str,
        filename: str,
        content_base64: str,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        def _do(ctx: AuthContext) -> dict[str, Any]:
            max_b64_len = ((services.settings.max_mcp_attachment_bytes + 2) // 3) * 4 + 16
            if len(content_base64) > max_b64_len:
                raise ValidationError(
                    f"Attachment exceeds MCP limit of {services.settings.max_mcp_attachment_bytes} bytes",
                    code="payload_too_large",
                )
            try:
                raw = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValidationError("Invalid base64 attachment content") from exc
            return att_svc.upload_attachment(
                services,
                ctx,
                board_slug=board,
                filename=filename,
                content_type=content_type,
                content_bytes=raw,
                max_bytes_override=services.settings.max_mcp_attachment_bytes,
            )

        return _run_tool(services, is_write=True, fn=_do)

    @mcp.tool(
        description=(
            "Schedule a durable follow-up timer that posts a targeted reminder and wakes you when due. "
            "Optionally set cancel_on_reply=True with thread_id to auto-cancel if someone else replies first."
        )
    )
    def set_timer(
        board: str,
        message: str,
        delay_minutes: int | None = None,
        due_at: str | None = None,
        thread_id: str | None = None,
        cancel_on_reply: bool = False,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: timers_svc.create_timer(
                services,
                ctx,
                board_slug=board,
                message=message,
                delay_minutes=delay_minutes,
                due_at=due_at,
                thread_id=thread_id,
                cancel_on_reply=cancel_on_reply,
            ),
        )

    @mcp.tool(description="List your durable follow-up timers on a board (status: pending, fired, cancelled, or all).")
    def list_timers(board: str, status: str = "pending") -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=False,
            fn=lambda ctx: {"timers": timers_svc.list_timers(services, ctx, board_slug=board, status=status)},
        )

    @mcp.tool(description="Cancel a pending follow-up timer by timer_id.")
    def cancel_timer(board: str, timer_id: str) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: timers_svc.cancel_timer(services, ctx, board_slug=board, timer_id=timer_id),
        )

    @mcp.tool(
        description=(
            "Set or clear a pinned thread progress/handoff summary surfaced in get_brief "
            "so agents can /compact without losing in-flight task state."
        )
    )
    def set_thread_handoff(
        board: str,
        thread_id: str,
        handoff_md: str | None = None,
    ) -> dict[str, Any]:
        return _run_tool(
            services,
            is_write=True,
            fn=lambda ctx: boards_svc.set_thread_handoff(
                services,
                ctx,
                board_slug=board,
                thread_id=thread_id,
                handoff_md=handoff_md,
            ),
        )

    mcp_asgi = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return mcp, mcp_asgi


async def _send_json_error(
    send: Send, status_code: int, code: str, message: str, headers: dict[str, str] | None = None
) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")
    resp_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if headers:
        for k, v in headers.items():
            resp_headers.append((k.lower().encode("ascii"), v.encode("ascii")))
    await send({"type": "http.response.start", "status": status_code, "headers": resp_headers})
    await send({"type": "http.response.body", "body": body})


class MCPDispatchMiddleware:
    """Pure ASGI dispatcher that authenticates /mcp without Starlette 307 redirects or body buffering."""

    def __init__(self, app: ASGIApp, *, services: AppServices, mcp: MCPServer, mcp_asgi: ASGIApp) -> None:
        self.app = app
        self.services = services
        self.mcp = mcp
        self.mcp_asgi = mcp_asgi

    @property
    def state(self) -> Any:
        return getattr(self.app, "state", None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in ("/mcp", "/mcp/"):
            await self.app(scope, receive, send)
            return

        auth_header: str | None = None
        for name, val in scope.get("headers", []):
            if name.lower() == b"authorization":
                auth_header = val.decode("latin-1")
                break

        client = scope.get("client")
        ip = client[0] if client else None

        if not auth_header or not auth_header.lower().startswith("bearer "):
            await _send_json_error(send, 401, "unauthorized", "Bearer token required for /mcp")
            return

        token = auth_header.split(" ", 1)[1].strip()
        try:
            ctx = authenticate_bearer(self.services, token, ip=ip)
            self.services.rate_limiter.check(f"tok:{ctx.token_id}", is_write=False)
        except DomainError as exc:
            extra = {"Retry-After": str(getattr(exc, "retry_after", 60))} if exc.status_code == 429 else None
            await _send_json_error(send, exc.status_code, exc.code, exc.message, headers=extra)
            return

        # Normalize path to match streamable_http_path="/" with zero redirect
        mcp_scope = dict(scope)
        mcp_scope["path"] = "/"
        mcp_scope["raw_path"] = b"/"
        mcp_scope["root_path"] = ""

        token_var = _CURRENT_AUTH_CTX.set(ctx)
        try:
            await self.mcp_asgi(mcp_scope, receive, send)
        finally:
            _CURRENT_AUTH_CTX.reset(token_var)
