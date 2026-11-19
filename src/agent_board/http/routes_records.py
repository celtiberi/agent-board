"""REST routes for decisions, issues, subscriptions, and owner admin operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import PlainTextResponse

from agent_board.backup import manager as backup_mgr
from agent_board.http.deps import get_services, require_api_read, require_api_write
from agent_board.http.models import (
    CloseIssueRequest,
    CreatePrincipalRequest,
    CreateSubscriptionRequest,
    CreateTokenRequest,
    OnboardAgentRequest,
    OpenIssueRequest,
    RecordDecisionRequest,
    ReopenIssueRequest,
    RevokeDecisionRequest,
    SetMembershipRequest,
    SetPrincipalTagsRequest,
    ShoutRequest,
    SupersedeDecisionRequest,
    UpdateIssueRequest,
    UpdatePrincipalRequest,
)
from agent_board.services import auth as auth_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services import onboarding as onboarding_svc
from agent_board.services.context import AppServices, AuthContext
from agent_board.store import boards as boards_store

router = APIRouter()


# --- Decisions ---


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


# --- Issues ---


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


# --- Subscriptions ---


@router.get("/subscriptions")
def list_subscriptions(
    all: bool = Query(default=False),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return {"subscriptions": delivery_svc.list_subscriptions(services, ctx, all_principals=all)}


@router.post("/subscriptions", status_code=201)
def create_subscription(
    body: CreateSubscriptionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return delivery_svc.create_subscription(
        services,
        ctx,
        url=body.url,
        boards=body.boards,
        filter_cfg=body.filter,
        auth_header=body.auth_header,
        hmac_secret=body.hmac_secret,
        min_interval_s=body.min_interval_s,
        debounce_s=body.debounce_s,
        payload_format=body.payload_format,
        principal_handle=body.principal_handle,
        idempotency_key=idempotency_key,
    )


@router.delete("/subscriptions/{sub_id}")
def delete_subscription(
    sub_id: str,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return delivery_svc.delete_subscription(services, ctx, sub_id=sub_id)


@router.post("/subscriptions/{sub_id}/enable")
def enable_subscription(
    sub_id: str,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return delivery_svc.reenable_subscription(services, ctx, sub_id=sub_id)


@router.post("/subscriptions/{sub_id}/test")
async def test_subscription(
    sub_id: str,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    from agent_board.errors import NotFoundError, PermissionDeniedError
    from agent_board.store import delivery as delivery_store

    sub = delivery_store.get_subscription_by_id(services.db.read_conn(), sub_id)
    if sub is None:
        raise NotFoundError(f"Subscription '{sub_id}' not found")
    if sub["principal_id"] != ctx.principal_id and not ctx.is_server_owner:
        raise PermissionDeniedError("Cannot test another principal's subscription")

    worker = services.nudge_worker
    if worker is None:
        from agent_board.nudger.worker import NudgeWorker

        worker = NudgeWorker(services)
    return await worker.send_test_nudge(sub_id=sub_id)


# --- Admin (Server Owner) ---


@router.get("/admin/principals")
def admin_list_principals(
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return {"principals": auth_svc.list_principals(services, ctx)}


@router.post("/admin/principals", status_code=201)
def admin_create_principal(
    body: CreatePrincipalRequest,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return auth_svc.create_principal(
        services,
        ctx,
        handle=body.handle,
        kind=body.kind,
        display_name=body.display_name,
        is_server_owner=body.is_server_owner,
        password=body.password,
        tags=body.tags,
    )


@router.put("/admin/principals/{handle}/tags")
def admin_set_principal_tags(
    handle: str,
    body: SetPrincipalTagsRequest,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return auth_svc.set_principal_tags(services, ctx, handle=handle, tags=body.tags)


@router.patch("/admin/principals/{handle}")
def admin_rename_principal(
    handle: str,
    body: UpdatePrincipalRequest,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return auth_svc.rename_principal(
        services,
        ctx,
        handle=handle,
        new_handle=body.new_handle,
        display_name=body.display_name,
    )


@router.post("/admin/shout")
def admin_shout(
    body: ShoutRequest,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return messages_svc.shout(
        services,
        ctx,
        body_md=body.body_md,
        tag=body.tag,
        all_boards=body.all_boards,
        exclude_boards=body.exclude_boards,
        thread_title=body.thread_title,
        wake=body.wake,
        dry_run=body.dry_run,
    )


@router.get("/admin/tokens")
def admin_list_tokens(
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return {"tokens": auth_svc.list_tokens(services, ctx)}


@router.post("/admin/tokens", status_code=201)
def admin_create_token(
    body: CreateTokenRequest,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return auth_svc.create_token(
        services,
        ctx,
        principal_handle=body.principal_handle,
        name=body.name,
        board_scope=body.board_scope,
        max_role=body.max_role,
        expires_at=body.expires_at,
    )


@router.post("/admin/tokens/{token_id}/revoke")
def admin_revoke_token(
    token_id: str,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return auth_svc.revoke_token(services, ctx, token_id=token_id)


@router.get("/admin/memberships")
def admin_list_memberships(
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    auth_svc.require_server_owner(ctx)
    rows = boards_store.list_all_memberships(services.db.read_conn())
    return {
        "memberships": [
            {
                "board_slug": r["board_slug"],
                "principal_handle": r["principal_handle"],
                "role": r["role"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


@router.post("/admin/memberships")
def admin_set_membership(
    body: SetMembershipRequest,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return auth_svc.set_membership(
        services,
        ctx,
        board_slug=body.board_slug,
        principal_handle=body.principal_handle,
        role=body.role,
    )


@router.delete("/admin/memberships/{board_slug}/{principal_handle}")
def admin_remove_membership(
    board_slug: str,
    principal_handle: str,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return auth_svc.remove_membership(
        services,
        ctx,
        board_slug=board_slug,
        principal_handle=principal_handle,
    )


@router.get("/admin/audit")
def admin_audit(
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return {"entries": auth_svc.list_audit_logs(services, ctx, limit=limit)}


@router.get("/admin/backups")
def admin_backups(
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    auth_svc.require_server_owner(ctx)
    return backup_mgr.get_backup_status(services)


@router.post("/admin/backups/run")
def admin_run_backup(
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    auth_svc.require_server_owner(ctx)
    return backup_mgr.run_backup(services)


@router.post("/admin/onboard", status_code=201)
async def admin_onboard(
    body: OnboardAgentRequest,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return await onboarding_svc.onboard_agent(
        services,
        ctx,
        handle=body.handle,
        boards=body.boards,
        display_name=body.display_name,
        role=body.role,
        tags=body.tags,
        strict_scope=body.strict_scope,
        rotate_tokens=body.rotate_tokens,
        webhook_url=body.webhook_url,
        webhook_auth_header=body.webhook_auth_header,
        skip_plain_notes=body.skip_plain_notes,
        test_webhook=body.test_webhook,
    )
