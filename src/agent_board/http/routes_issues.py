"""REST routes for board issues."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query

from agent_board.http.deps import get_services, require_api_read, require_api_write
from agent_board.http.models import (
    CloseIssueRequest,
    OpenIssueRequest,
    ReopenIssueRequest,
    UpdateIssueRequest,
)
from agent_board.services import issues as issues_svc
from agent_board.services.context import AppServices, AuthContext

router = APIRouter()


@router.get("/boards/{slug}/issues")
def list_issues(
    slug: str,
    status: str = Query(default="active"),
    severity: str | None = Query(default=None),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return {"issues": issues_svc.list_issues(services, ctx, board_slug=slug, status=status, severity=severity)}


@router.post("/boards/{slug}/issues", status_code=201)
def open_issue(
    slug: str,
    body: OpenIssueRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return issues_svc.open_issue(
        services,
        ctx,
        board_slug=slug,
        title=body.title,
        body_md=body.body_md,
        severity=body.severity,
        assignee=body.assignee,
        tags=body.tags,
        thread_id=body.thread_id,
        idempotency_key=idempotency_key,
    )


@router.get("/boards/{slug}/issues/{ref}")
def get_issue(
    slug: str,
    ref: str,
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return issues_svc.get_issue_detail(services, ctx, board_slug=slug, ref=ref)


@router.patch("/boards/{slug}/issues/{ref}")
def update_issue(
    slug: str,
    ref: str,
    body: UpdateIssueRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return issues_svc.update_issue(
        services,
        ctx,
        board_slug=slug,
        ref=ref,
        status=body.status,
        assignee=body.assignee,
        comment=body.comment,
        idempotency_key=idempotency_key,
    )


@router.post("/boards/{slug}/issues/{ref}/close")
def close_issue(
    slug: str,
    ref: str,
    body: CloseIssueRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return issues_svc.close_issue(
        services,
        ctx,
        board_slug=slug,
        ref=ref,
        resolution=body.resolution,
        status=body.status,
        idempotency_key=idempotency_key,
    )


@router.post("/boards/{slug}/issues/{ref}/reopen")
def reopen_issue(
    slug: str,
    ref: str,
    body: ReopenIssueRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return issues_svc.reopen_issue(
        services,
        ctx,
        board_slug=slug,
        ref=ref,
        reason=body.reason,
        idempotency_key=idempotency_key,
    )
