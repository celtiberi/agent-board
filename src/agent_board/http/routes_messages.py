"""REST routes for messages, edits, redaction, and attachments."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Header, Query, UploadFile
from fastapi.responses import FileResponse

from agent_board.http.deps import get_services, require_api_read, require_api_write
from agent_board.http.models import EditMessageRequest, PostMessageRequest, RedactMessageRequest
from agent_board.services import attachments as att_svc
from agent_board.services import messages as messages_svc
from agent_board.services.context import AppServices, AuthContext

router = APIRouter()


@router.get("/boards/{slug}/messages")
def read_messages(
    slug: str,
    since_seq: int | None = Query(default=None, ge=0),
    thread: str | None = Query(default=None),
    types: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else None
    return messages_svc.read_messages(
        services,
        ctx,
        board_slug=slug,
        since_seq=since_seq,
        thread_id=thread,
        types=type_list,
        limit=limit,
    )


@router.post("/boards/{slug}/messages", status_code=201)
def post_message(
    slug: str,
    body: PostMessageRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return messages_svc.post_message(
        services,
        ctx,
        board_slug=slug,
        msg_type=body.type,
        priority=body.priority,
        body_md=body.body_md,
        thread_id=body.thread_id,
        thread_title=body.thread_title,
        to=body.to,
        cc=body.cc,
        data=body.data,
        reply_to_id=body.reply_to_id,
        attachment_ids=body.attachment_ids,
        idempotency_key=idempotency_key,
    )


@router.get("/boards/{slug}/messages/{message_id}")
def get_message(
    slug: str,
    message_id: str,
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return messages_svc.get_message_with_history(services, ctx, board_slug=slug, message_id=message_id)


@router.post("/boards/{slug}/messages/{message_id}/edit")
def edit_message(
    slug: str,
    message_id: str,
    body: EditMessageRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return messages_svc.edit_message(
        services,
        ctx,
        board_slug=slug,
        message_id=message_id,
        body_md=body.body_md,
        to=body.to,
        cc=body.cc,
        data=body.data,
        idempotency_key=idempotency_key,
    )


@router.post("/boards/{slug}/messages/{message_id}/redact")
def redact_message(
    slug: str,
    message_id: str,
    body: RedactMessageRequest,
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    return messages_svc.redact_message(
        services,
        ctx,
        board_slug=slug,
        message_id=message_id,
        reason=body.reason,
    )


@router.post("/boards/{slug}/attachments", status_code=201)
async def upload_attachment(
    slug: str,
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_api_write),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    content = await file.read()
    return att_svc.upload_attachment(
        services,
        ctx,
        board_slug=slug,
        filename=file.filename or "attachment.bin",
        content_type=file.content_type,
        content_bytes=content,
    )


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: str,
    ctx: AuthContext = Depends(require_api_read),
    services: AppServices = Depends(get_services),
) -> FileResponse:
    meta, path = att_svc.get_attachment_for_download(services, ctx, attachment_id=attachment_id)
    return FileResponse(
        path=path,
        media_type=meta["content_type"],
        filename=meta["filename"],
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff"},
    )
