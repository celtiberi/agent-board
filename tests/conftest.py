"""Shared pytest fixtures for unit, contract, and e2e tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_board.config import Settings, load_settings
from agent_board.services import boards as boards_svc
from agent_board.services.auth import create_principal, create_token, set_membership
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.seed import ensure_owner_and_ops
from agent_board.store import principals as principals_store


@dataclass
class BoardTestEnv:
    settings: Settings
    services: AppServices
    owner_ctx: AuthContext
    owner_token: str
    bob_token: str
    dev_token: str
    ro_token: str
    bob_ctx: AuthContext
    dev_ctx: AuthContext
    ro_ctx: AuthContext


@pytest.fixture
def env(tmp_path: Path) -> BoardTestEnv:
    settings = load_settings(data_dir=tmp_path / "data")
    services = AppServices.create(settings)
    ensure_owner_and_ops(services, owner_password="super-secret-password-123")

    owner_row = principals_store.get_first_server_owner(services.db.read_conn())
    assert owner_row is not None
    owner_ctx = AuthContext(
        principal_id=owner_row["id"],
        handle=owner_row["handle"],
        kind="human",
        display_name=owner_row["display_name"],
        is_server_owner=True,
        auth_kind="session",
        board_scope=("*",),
        max_role="owner",
    )

    create_principal(services, owner_ctx, handle="bob", kind="agent", display_name="Bob")
    create_principal(services, owner_ctx, handle="mac-dev-ai", kind="agent", display_name="Dev AI")
    create_principal(services, owner_ctx, handle="viewer", kind="agent", display_name="Viewer")

    boards_svc.create_board(
        services,
        owner_ctx,
        slug="alpha",
        name="Alpha Project",
        purpose="Test coordination board for Alpha.",
    )
    set_membership(services, owner_ctx, board_slug="alpha", principal_handle="bob", role="agent")
    set_membership(services, owner_ctx, board_slug="alpha", principal_handle="mac-dev-ai", role="agent")
    set_membership(services, owner_ctx, board_slug="alpha", principal_handle="viewer", role="readonly")

    owner_tok = create_token(
        services, owner_ctx, principal_handle=owner_row["handle"], name="owner-cli", board_scope=["*"], max_role="owner"
    )["token"]
    bob_tok = create_token(
        services, owner_ctx, principal_handle="bob", name="bob-prod", board_scope=["*"], max_role="agent"
    )["token"]
    dev_tok = create_token(
        services, owner_ctx, principal_handle="mac-dev-ai", name="dev-local", board_scope=["*"], max_role="agent"
    )["token"]
    ro_tok = create_token(
        services, owner_ctx, principal_handle="viewer", name="viewer-ro", board_scope=["*"], max_role="readonly"
    )["token"]

    from agent_board.services.auth import authenticate_bearer

    yield BoardTestEnv(
        settings=settings,
        services=services,
        owner_ctx=owner_ctx,
        owner_token=owner_tok,
        bob_token=bob_tok,
        dev_token=dev_tok,
        ro_token=ro_tok,
        bob_ctx=authenticate_bearer(services, bob_tok),
        dev_ctx=authenticate_bearer(services, dev_tok),
        ro_ctx=authenticate_bearer(services, ro_tok),
    )
    services.close()
