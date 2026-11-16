"""REST routes for identity, boards, brief, threads, cursors, and search."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import PlainTextResponse

from agent_board.http.deps import get_services, require_api_read, require_api_write
from agent_board.http.models import AckRequest, CreateBoardRequest, CreateThreadRequest, UpdateBoardRequest
from agent_board.services import boards as boards_svc
from agent_board.services import brief as brief_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import messages as messages_svc
from agent_board.services import search as search_svc
from agent_board.services.context import AppServices, AuthContext

router = APIRouter()


@router.get("/me")
def get_me(
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    boards = boards_svc.list_boards(services, ctx)
    return {
        "principal": {
            "id": ctx.principal_id,
            "handle": ctx.handle,
            "kind": ctx.kind,
            "display_name": ctx.display_name,
            "is_server_owner": ctx.is_server_owner,
        },
        "auth_kind": ctx.auth_kind,
        "token_prefix": ctx.token_prefix,
        "board_scope": list(ctx.board_scope),
        "max_role": ctx.max_role,
        "boards": boards,
    }


@router.get("/boards")
def list_boards(
    include_archived: bool = Query(default=False),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return {"boards": boards_svc.list_boards(services, ctx, include_archived=include_archived)}


@router.post("/boards", status_code=201)
def create_board(
    body: CreateBoardRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return boards_svc.create_board(
        services,
        ctx,
        slug=body.slug,
        name=body.name,
        purpose=body.purpose,
        settings=body.settings,
        idempotency_key=idempotency_key,
    )


@router.get("/boards/{slug}")
def get_board(
    slug: str,
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return boards_svc.get_board(services, ctx, board_slug=slug)


@router.patch("/boards/{slug}")
def update_board(
    slug: str,
    body: UpdateBoardRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return boards_svc.update_board(
        services,
        ctx,
        board_slug=slug,
        name=body.name,
        purpose=body.purpose,
        settings=body.settings,
        archived=body.archived,
        idempotency_key=idempotency_key,
    )


@router.get("/boards/{slug}/brief", response_model=None)
def get_brief(
    slug: str,
    format: str = Query(default="markdown"),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> Any:
    data = brief_svc.get_brief(services, ctx, board_slug=slug)
    if format == "json":
        return data
    return PlainTextResponse(data["markdown"], media_type="text/markdown; charset=utf-8")


@router.get("/boards/{slug}/threads")
def list_threads(
    slug: str,
    state: str = Query(default="open"),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return {"threads": boards_svc.list_threads(services, ctx, board_slug=slug, state=state)}


@router.post("/boards/{slug}/threads", status_code=201)
def create_thread(
    slug: str,
    body: CreateThreadRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return boards_svc.create_thread(
        services,
        ctx,
        board_slug=slug,
        title=body.title,
        first_message=body.first_message.model_dump() if body.first_message else None,
        idempotency_key=idempotency_key,
    )


@router.get("/boards/{slug}/threads/{thread_id}")
def get_thread(
    slug: str,
    thread_id: str,
    since_seq: int | None = Query(default=None, ge=0),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return messages_svc.get_thread_snapshot(services, ctx, board_slug=slug, thread_id=thread_id, since_seq=since_seq)


@router.post("/boards/{slug}/threads/{thread_id}/close")
def close_thread(
    slug: str,
    thread_id: str,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return boards_svc.close_thread(services, ctx, board_slug=slug, thread_id=thread_id, closed=True)


@router.post("/boards/{slug}/threads/{thread_id}/reopen")
def reopen_thread(
    slug: str,
    thread_id: str,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return boards_svc.close_thread(services, ctx, board_slug=slug, thread_id=thread_id, closed=False)


@router.post("/boards/{slug}/ack")
def ack_board(
    slug: str,
    body: AckRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return delivery_svc.ack_cursor(
        services,
        ctx,
        board_slug=slug,
        read_seq=body.read_seq,
        idempotency_key=idempotency_key,
    )


@router.get("/search")
def search(
    q: str = Query(...),
    board: str | None = Query(default=None),
    types: str | None = Query(default=None),
    author: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    return search_svc.search(
        services,
        ctx,
        query=q,
        board_slug=board,
        types=type_list,
        author_handle=author,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
