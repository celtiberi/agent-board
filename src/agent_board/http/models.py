"""Pydantic request schemas for REST API endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


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
    model_config = ConfigDict(populate_by_name=True)

    thread_id: str | None = None
    thread_title: str | None = None
    type: str = "note"
    body_md: str = Field(validation_alias=AliasChoices("body_md", "body", "content", "message"))
    data: dict[str, Any] | None = None
    reply_to_id: str | None = None
    attachment_ids: list[str] | None = None


class EditMessageRequest(BaseModel):
    body_md: str
    data: dict[str, Any] | None = None


class RedactMessageRequest(BaseModel):
    reason: str


class AckRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    read_seq: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("read_seq", "seq", "latest_seq", "up_to_seq"),
    )


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
    model_config = ConfigDict(populate_by_name=True)

    title: str
    body_md: str = Field(validation_alias=AliasChoices("body_md", "body", "description"))
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


class UpdateSubscriptionRequest(BaseModel):
    url: str | None = None
    boards: list[str] | None = None
    filter: dict[str, Any] | None = None
    auth_header: str | None = None
    clear_auth_header: bool = False
    min_interval_s: int | None = None
    debounce_s: int | None = None


class CreatePrincipalRequest(BaseModel):
    handle: str
    kind: str = "agent"
    display_name: str
    is_server_owner: bool = False
    password: str | None = None
    tags: list[str] | None = None


class SetPrincipalTagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)


class UpdatePrincipalRequest(BaseModel):
    new_handle: str | None = None
    display_name: str | None = None


class ShoutRequest(BaseModel):
    body_md: str
    tag: str | None = None
    all_boards: bool = False
    exclude_boards: list[str] = Field(default_factory=list)
    thread_title: str = "general"
    wake: bool = True
    dry_run: bool = False


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


class OnboardAgentRequest(BaseModel):
    handle: str
    boards: list[str]
    display_name: str | None = None
    role: str = "agent"
    tags: list[str] | None = None
    strict_scope: bool = True
    rotate_tokens: bool = True
    webhook_url: str | None = None
    webhook_auth_header: str | None = None
    skip_plain_notes: bool = True
    test_webhook: bool = True
