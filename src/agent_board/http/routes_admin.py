"""REST routes for server-owner administration operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from agent_board.backup import manager as backup_mgr
from agent_board.http.deps import get_services, require_api_read, require_api_write
from agent_board.http.models import (
    CreatePrincipalRequest,
    CreateTokenRequest,
    OnboardAgentRequest,
    SetMembershipRequest,
    SetPrincipalTagsRequest,
    ShoutRequest,
    UpdatePrincipalRequest,
)
from agent_board.services import auth as auth_svc
from agent_board.services import onboarding as onboarding_svc
from agent_board.services import shout as shout_svc
from agent_board.services.context import AppServices, AuthContext
from agent_board.store import boards as boards_store

router = APIRouter()


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
    return shout_svc.shout(
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
