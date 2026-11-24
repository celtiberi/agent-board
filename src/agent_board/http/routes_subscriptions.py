"""REST routes for webhook subscriptions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query

from agent_board.errors import NotFoundError, PermissionDeniedError
from agent_board.http.deps import get_services, require_api_read, require_api_write
from agent_board.http.models import CreateSubscriptionRequest, UpdateSubscriptionRequest
from agent_board.services import delivery as delivery_svc
from agent_board.services.context import AppServices, AuthContext
from agent_board.store import delivery as delivery_store

router = APIRouter()


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


@router.patch("/subscriptions/{sub_id}")
def update_subscription(
    sub_id: str,
    body: UpdateSubscriptionRequest,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return delivery_svc.update_subscription(
        services,
        ctx,
        sub_id=sub_id,
        url=body.url,
        boards=body.boards,
        filter_cfg=body.filter,
        auth_header=body.auth_header,
        clear_auth_header=body.clear_auth_header,
        min_interval_s=body.min_interval_s,
        debounce_s=body.debounce_s,
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
    if not ctx.has_server_owner_privilege and ctx.max_role == "readonly":
        raise PermissionDeniedError("Readonly tokens cannot manage subscriptions")
    sub = delivery_store.get_subscription_by_id(services.db.read_conn(), sub_id)
    if sub is None:
        raise NotFoundError(f"Subscription '{sub_id}' not found")
    if sub["principal_id"] != ctx.principal_id and not ctx.has_server_owner_privilege:
        raise PermissionDeniedError("Cannot test another principal's subscription")

    worker = services.nudge_worker
    if worker is None:
        from agent_board.nudger.worker import NudgeWorker

        worker = NudgeWorker(services)
    return await worker.send_test_nudge(sub_id=sub_id)
