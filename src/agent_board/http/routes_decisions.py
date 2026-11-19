"""REST routes for board decisions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import PlainTextResponse

from agent_board.http.deps import get_services, require_api_read, require_api_write
from agent_board.http.models import (
    RecordDecisionRequest,
    RevokeDecisionRequest,
    SupersedeDecisionRequest,
)
from agent_board.services import decisions as decisions_svc
from agent_board.services.context import AppServices, AuthContext

router = APIRouter()


@router.get("/boards/{slug}/decisions")
def list_decisions(
    slug: str,
    status: str = Query(default="active"),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return {"decisions": decisions_svc.list_decisions(services, ctx, board_slug=slug, status=status)}


@router.get("/boards/{slug}/decisions/export.md", response_class=PlainTextResponse)
def export_decisions(
    slug: str,
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> PlainTextResponse:
    md = decisions_svc.export_decisions_md(services, ctx, board_slug=slug)
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")


@router.get("/boards/{slug}/decisions/{ref}")
def get_decision(
    slug: str,
    ref: str,
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return decisions_svc.get_decision(services, ctx, board_slug=slug, ref=ref)


@router.post("/boards/{slug}/decisions", status_code=201)
def record_decision(
    slug: str,
    body: RecordDecisionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return decisions_svc.record_decision(
        services,
        ctx,
        board_slug=slug,
        title=body.title,
        statement=body.statement,
        decided_by=body.decided_by,
        decided_on=body.decided_on,
        rationale=body.rationale,
        tags=body.tags,
        thread_title=body.thread_title,
        idempotency_key=idempotency_key,
    )


@router.post("/boards/{slug}/decisions/{ref}/supersede", status_code=201)
def supersede_decision(
    slug: str,
    ref: str,
    body: SupersedeDecisionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return decisions_svc.supersede_decision(
        services,
        ctx,
        board_slug=slug,
        old_ref=ref,
        title=body.title,
        statement=body.statement,
        decided_by=body.decided_by,
        decided_on=body.decided_on,
        reason=body.reason,
        rationale=body.rationale,
        tags=body.tags,
        thread_title=body.thread_title,
        idempotency_key=idempotency_key,
    )


@router.post("/boards/{slug}/decisions/{ref}/revoke")
def revoke_decision(
    slug: str,
    ref: str,
    body: RevokeDecisionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return decisions_svc.revoke_decision(
        services,
        ctx,
        board_slug=slug,
        ref=ref,
        reason=body.reason,
        idempotency_key=idempotency_key,
    )
