"""Pydantic request schemas for REST API endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateBoardRequest(BaseModel):
    slug: str
    name: str
    purpose: str
    settings: dict[str, Any] | None = None


class UpdateBoardRequest(BaseModel):
    name: str | None = None
    purpose: str | None = None
    settings: dict[str, Any] | None = None
    archived: bool | None = None


class FirstMessageInput(BaseModel):
    type: str = "note"
    body_md: str
    data: dict[str, Any] | None = None
    reply_to_id: str | None = None
    attachment_ids: list[str] | None = None


class CreateThreadRequest(BaseModel):
    title: str
    first_message: FirstMessageInput | None = None


class PostMessageRequest(BaseModel):
    thread_id: str | None = None
    thread_title: str | None = None
    type: str = "note"
    body_md: str
    data: dict[str, Any] | None = None
    reply_to_id: str | None = None
    attachment_ids: list[str] | None = None


class EditMessageRequest(BaseModel):
    body_md: str
    data: dict[str, Any] | None = None


class RedactMessageRequest(BaseModel):
    reason: str


class AckRequest(BaseModel):
    read_seq: int = Field(ge=0)


class RecordDecisionRequest(BaseModel):
    title: str
    statement: str
    decided_by: str
    decided_on: str
    rationale: str | None = None
    tags: list[str] | None = None
    thread_title: str = "decisions"


class SupersedeDecisionRequest(BaseModel):
    title: str
    statement: str
    decided_by: str
    decided_on: str
    reason: str
    rationale: str | None = None
    tags: list[str] | None = None
    thread_title: str = "decisions"


class RevokeDecisionRequest(BaseModel):
    reason: str


class OpenIssueRequest(BaseModel):
    title: str
    body_md: str
    severity: str = "normal"
    assignee: str | None = None
    tags: list[str] | None = None
    thread_id: str | None = None


class UpdateIssueRequest(BaseModel):
    status: str | None = None
    assignee: str | None = None
    comment: str | None = None


class CloseIssueRequest(BaseModel):
    resolution: str
    status: str = "resolved"


class ReopenIssueRequest(BaseModel):
    reason: str


class CreateSubscriptionRequest(BaseModel):
    url: str
    boards: list[str] | None = None
    filter: dict[str, Any] | None = None
    auth_header: str | None = None
    hmac_secret: str | None = None
    min_interval_s: int = 60
    debounce_s: int = 10
    payload_format: str = "default"
    principal_handle: str | None = None


class CreatePrincipalRequest(BaseModel):
    handle: str
    kind: str = "agent"
    display_name: str
    is_server_owner: bool = False
    password: str | None = None


class CreateTokenRequest(BaseModel):
    principal_handle: str
    name: str
    board_scope: list[str] = Field(default_factory=lambda: ["*"])
    max_role: str = "agent"
    expires_at: str | None = None


class SetMembershipRequest(BaseModel):
    board_slug: str
    principal_handle: str
    role: str
