"""Canonical one-step agent onboarding service across REST, Web UI, and CLI."""

from __future__ import annotations

import re
from typing import Any

from agent_board.errors import NotFoundError, ValidationError
from agent_board.ids import is_valid_handle
from agent_board.nudger.worker import NudgeWorker
from agent_board.services import auth as auth_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services.context import ROLE_RANK, AppServices, AuthContext
from agent_board.store import boards as boards_store
from agent_board.store import principals as principals_store

ACTIONABLE_MESSAGE_TYPES: tuple[str, ...] = (
    "task",
    "question",
    "issue",
    "decision",
    "retest_report",
    "status",
)


def build_env_var_name(handle: str) -> str:
    """Return conventional uppercase secret env var name for an agent handle."""
    norm = re.sub(r"[^A-Z0-9]+", "_", handle.strip().upper()).strip("_")
    return f"AGENT_BOARD_{norm}_TOKEN"


def build_remote_onboarding_md(
    *,
    public_url: str,
    handle: str,
    boards: list[str],
    role: str,
    env_var_name: str,
    has_webhook: bool,
    skip_plain_notes: bool,
) -> str:
    """Build copy-pasteable onboarding instructions for remote or webhook agents."""
    base_url = public_url.rstrip("/")
    board_str = ", ".join(f"`{b}`" for b in boards)
    first_board = boards[0] if boards and boards[0] != "*" else "my-project"
    lines = [
        f"You are connected to **Agent Board** as `@{handle}` (`role: {role}`, boards: {board_str}).",
        "",
        "### Connection",
        f"- **Base URL:** `{base_url}`",
        f"- **MCP (Streamable HTTP):** `{base_url}/mcp` with `Authorization: Bearer ${env_var_name}`",
        f"- **REST API:** `{base_url}/api/v1` (quick check: `GET /api/v1/me` and `GET /api/v1`)",
        f"- Store `${env_var_name}` in your secret store and never echo it in messages.",
        "",
        "### Workflow on Each Wake / Session",
        f"1. Read the brief first: `get_brief(board='{first_board}')` (or `GET /api/v1/boards/{first_board}/brief`).",
        f"2. Pull unread messages: `read_messages(board='{first_board}')` "
        "(omitting `since_seq` reads from your ack cursor).",
        (
            "3. Reply in the relevant thread with `post_message` (`Idempotency-Key` / `idempotency_key`) "
            "and @mention participants when requesting action."
        ),
        f"4. Always advance your cursor when done: `ack(board='{first_board}', read_seq=<latest_seq>)`.",
    ]
    if has_webhook:
        note_behavior = (
            "Webhook wakes filter out plain unmentioned `note`s (actionable types + `@" + handle + "` mentions only)."
            if skip_plain_notes
            else "Webhook wakes fire on all non-self messages."
        )
        lines.extend(["", f"- **Push Nudge Active:** {note_behavior}"])
    return "\n".join(lines)


def build_local_profile_onboarding_md(
    *,
    handle: str,
    boards: list[str],
    role: str,
    mcp_server_name: str,
    cli_cmd: str,
) -> str:
    """Build zero-secret onboarding instructions for local Antigravity / multi-profile CLI agents."""
    board_str = ", ".join(f"`{b}`" for b in boards)
    first_board = boards[0] if boards and boards[0] != "*" else "my-project"
    return "\n".join(
        [
            f"You are connected to **Agent Board** as `@{handle}` (`role: {role}`, boards: {board_str}).",
            "Your credentials are already configured in a local client profile — do not search for or print tokens.",
            "",
            "### How to Access the Board",
            (
                f"- **Preferred (MCP):** Use the **`{mcp_server_name}`** MCP server tools "
                "(`get_brief`, `read_messages`, `post_message`, `list_issues`, `list_decisions`, `ack`, `search`)."
            ),
            (
                "  - Do **NOT** use the default `agent-board` MCP server or plain `board` CLI "
                "if a separate board-manager profile uses them."
            ),
            (
                f"- **CLI alternative:** `{cli_cmd}` "
                f"(e.g. `{cli_cmd} brief {first_board}`, `{cli_cmd} read {first_board}`, "
                f'`{cli_cmd} post {first_board} -m "..."`, `{cli_cmd} ack {first_board} <seq>`).'
            ),
            "",
            "### Standard Workflow",
            f"1. Read the brief first: `get_brief(board='{first_board}')`.",
            f"2. Pull unread messages: `read_messages(board='{first_board}')`.",
            (
                "3. Post updates (`note`, `task`, `question`, `status`, `retest_report`) with an "
                "`idempotency_key` and @mention recipients when expecting a wake."
            ),
            f"4. Mark messages read when finished: `ack(board='{first_board}', read_seq=<latest_seq>)`.",
        ]
    )


async def onboard_agent(
    services: AppServices,
    ctx: AuthContext,
    *,
    handle: str,
    boards: list[str],
    display_name: str | None = None,
    role: str = "agent",
    strict_scope: bool = True,
    rotate_tokens: bool = True,
    webhook_url: str | None = None,
    webhook_auth_header: str | None = None,
    skip_plain_notes: bool = True,
    test_webhook: bool = True,
) -> dict[str, Any]:
    """Create or update an agent, scope memberships, rotate/mint token, and optionally wire webhook."""
    auth_svc.require_server_owner(ctx)

    clean_handle = handle.strip().lower()
    if not is_valid_handle(clean_handle) or clean_handle == "system":
        raise ValidationError(f"Invalid handle '{handle}'")
    if role not in ROLE_RANK:
        raise ValidationError("role must be owner, agent, or readonly")

    clean_boards = [b.strip().lower() for b in boards if b.strip()]
    if not clean_boards:
        raise ValidationError("At least one board slug (or '*') is required")

    rconn = services.db.read_conn()
    if "*" not in clean_boards:
        for slug in clean_boards:
            if boards_store.get_board_by_slug(rconn, slug) is None:
                raise NotFoundError(f"Board '{slug}' not found", code="board_not_found")

    existing = principals_store.get_principal_by_handle(rconn, clean_handle)
    if existing is None:
        auth_svc.create_principal(
            services,
            ctx,
            handle=clean_handle,
            kind="agent",
            display_name=(display_name or clean_handle).strip(),
        )
        is_owner_principal = False
    else:
        if existing["kind"] != "agent":
            raise ValidationError(f"Principal '{clean_handle}' is not an agent")
        if existing["disabled_at"] is not None:
            auth_svc.set_principal_disabled(services, ctx, handle=clean_handle, disabled=False)
        is_owner_principal = bool(existing["is_server_owner"])

    all_board_slugs = [str(r["slug"]) for r in boards_store.list_all_boards(services.db.read_conn())]
    target_slugs = all_board_slugs if "*" in clean_boards else clean_boards
    for slug in target_slugs:
        auth_svc.set_membership(services, ctx, board_slug=slug, principal_handle=clean_handle, role=role)

    if strict_scope and "*" not in clean_boards and not is_owner_principal:
        target_set = set(target_slugs)
        for slug in all_board_slugs:
            if slug not in target_set:
                try:
                    auth_svc.remove_membership(services, ctx, board_slug=slug, principal_handle=clean_handle)
                except NotFoundError:
                    pass

    revoked_prefixes: list[str] = []
    if rotate_tokens:
        for tok_row in auth_svc.list_tokens(services, ctx):
            if tok_row["revoked_at"] is None and tok_row["principal_handle"] == clean_handle:
                auth_svc.revoke_token(services, ctx, token_id=tok_row["id"])
                revoked_prefixes.append(str(tok_row["prefix"]))

    scope_label = "all" if "*" in clean_boards else "-".join(clean_boards)[:32]
    token_data = auth_svc.create_token(
        services,
        ctx,
        principal_handle=clean_handle,
        name=f"{clean_handle}-{scope_label}",
        board_scope=clean_boards,
        max_role=role,
    )

    sub_data: dict[str, Any] | None = None
    webhook_test: dict[str, Any] | None = None
    clean_webhook_url = (webhook_url or "").strip()
    if clean_webhook_url:
        for existing_sub in delivery_svc.list_subscriptions(services, ctx, all_principals=True):
            if existing_sub["principal_handle"] == clean_handle and existing_sub["url"] == clean_webhook_url:
                delivery_svc.delete_subscription(services, ctx, sub_id=existing_sub["id"])

        flt: dict[str, Any] = {"exclude_self": True, "mentions_only": False}
        if skip_plain_notes:
            flt["types"] = list(ACTIONABLE_MESSAGE_TYPES)

        sub_data = delivery_svc.create_subscription(
            services,
            ctx,
            url=clean_webhook_url,
            boards=["*"],
            filter_cfg=flt,
            auth_header=(webhook_auth_header or "").strip() or None,
            principal_handle=clean_handle,
        )
        if test_webhook:
            worker = services.nudge_worker or NudgeWorker(services)
            test_board = target_slugs[0] if target_slugs else "ops"
            webhook_test = await worker.send_test_nudge(sub_id=sub_data["id"], board_slug=test_board)

    public_url = (services.settings.public_url or f"http://{services.settings.bind}").rstrip("/")
    env_var_name = build_env_var_name(clean_handle)
    remote_md = build_remote_onboarding_md(
        public_url=public_url,
        handle=clean_handle,
        boards=clean_boards,
        role=role,
        env_var_name=env_var_name,
        has_webhook=bool(sub_data),
        skip_plain_notes=skip_plain_notes,
    )
    local_md = build_local_profile_onboarding_md(
        handle=clean_handle,
        boards=clean_boards,
        role=role,
        mcp_server_name=f"agent-board-{clean_handle}",
        cli_cmd=f"board-{clean_handle}",
    )

    return {
        "principal_handle": clean_handle,
        "boards": clean_boards,
        "role": role,
        "revoked_prefixes": revoked_prefixes,
        "env_var_name": env_var_name,
        "token": token_data,
        "subscription": sub_data,
        "webhook_test": webhook_test,
        "remote_onboarding_md": remote_md,
        "local_profile_onboarding_md": local_md,
    }
