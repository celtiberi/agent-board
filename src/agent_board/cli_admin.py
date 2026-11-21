"""Server-owner `board admin ...` CLI subcommands."""

from __future__ import annotations

import json
from typing import Any

import httpx
import typer

from agent_board.cli_runtime import bind_antigravity_conversation, install_local_agent_profile

admin_cli = typer.Typer(help="Server owner administration commands.")


def _cli_helpers() -> tuple[Any, Any, Any]:
    from agent_board.board_cli import _check_resp, _client, _load_config

    return _load_config, _client, _check_resp


@admin_cli.command("create-board")
def admin_create_board(
    slug: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    purpose: str = typer.Option(..., "--purpose"),
) -> None:
    """Create a new board."""
    _, _client, _check_resp = _cli_helpers()
    with _client() as c:
        data = _check_resp(c.post("/boards", json={"slug": slug, "name": name, "purpose": purpose}))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("principals")
def admin_principals() -> None:
    """List principals."""
    _, _client, _check_resp = _cli_helpers()
    with _client() as c:
        data = _check_resp(c.get("/admin/principals"))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("create-principal")
def admin_create_principal(
    handle: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    kind: str = typer.Option("agent", "--kind"),
    owner: bool = typer.Option(False, "--owner/--no-owner"),
    tag: list[str] = typer.Option([], "--tag", "-T", help="Capability/group tag(s) (e.g. dev, qa)."),
) -> None:
    """Create a principal."""
    _, _client, _check_resp = _cli_helpers()
    payload: dict[str, Any] = {"handle": handle, "kind": kind, "display_name": name, "is_server_owner": owner}
    if tag:
        payload["tags"] = [s.strip() for raw in tag for s in raw.split(",") if s.strip()]
    with _client() as c:
        data = _check_resp(c.post("/admin/principals", json=payload))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("set-tags")
def admin_set_tags(
    handle: str = typer.Argument(...),
    tags: list[str] = typer.Argument(None, help="Capability tags (e.g. dev qa)."),
    clear: bool = typer.Option(False, "--clear", help="Remove all tags from the principal."),
) -> None:
    """Set or clear capability/group tags on a principal."""
    _, _client, _check_resp = _cli_helpers()
    if clear and tags:
        typer.echo("Error: pass either tag names or --clear, not both.", err=True)
        raise typer.Exit(1)
    if not clear and not tags:
        typer.echo("Error: pass one or more tags or --clear.", err=True)
        raise typer.Exit(1)
    clean_tags = [] if clear else [s.strip() for raw in tags for s in raw.split(",") if s.strip()]
    with _client() as c:
        data = _check_resp(c.put(f"/admin/principals/{handle}/tags", json={"tags": clean_tags}))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("rename-principal")
def admin_rename_principal(
    handle: str = typer.Argument(...),
    new_handle: str | None = typer.Option(None, "--new-handle", help="New lowercase @handle."),
    name: str | None = typer.Option(None, "--name", help="New display name."),
) -> None:
    """Rename a principal's @handle and/or display name while keeping ID, tokens, and memberships."""
    _, _client, _check_resp = _cli_helpers()
    if new_handle is None and name is None:
        typer.echo("Error: pass --new-handle and/or --name.", err=True)
        raise typer.Exit(1)
    payload: dict[str, Any] = {}
    if new_handle is not None:
        payload["new_handle"] = new_handle
    if name is not None:
        payload["display_name"] = name
    with _client() as c:
        data = _check_resp(c.patch(f"/admin/principals/{handle}", json=payload))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("shout")
def admin_shout(
    message: str = typer.Option(..., "--message", "-m", help="Markdown announcement to post across matching boards."),
    tag: str | None = typer.Option(None, "--tag", "-T", help="Target active members with this tag (e.g. dev)."),
    all_boards: bool = typer.Option(False, "--all", help="Target all active project boards."),
    exclude_board: list[str] = typer.Option([], "--exclude-board", help="Board slug(s) to skip."),
    thread: str = typer.Option("general", "--thread", help="Thread title on each target board."),
    wake: bool = typer.Option(True, "--wake/--quiet", help="Send actionable wake vs. silent note."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview matching boards and handles without posting."),
) -> None:
    """Broadcast a server-owner announcement across project boards filtered by tag or to all boards."""
    _, _client, _check_resp = _cli_helpers()
    if bool(tag and tag.strip()) == bool(all_boards):
        typer.echo("Error: specify either --tag or --all.", err=True)
        raise typer.Exit(1)
    payload: dict[str, Any] = {
        "body_md": message,
        "tag": tag.strip() if tag else None,
        "all_boards": all_boards,
        "exclude_boards": [s.strip() for raw in exclude_board for s in raw.split(",") if s.strip()],
        "thread_title": thread,
        "wake": wake,
        "dry_run": dry_run,
    }
    with _client() as c:
        data = _check_resp(c.post("/admin/shout", json=payload))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("memberships")
def admin_memberships() -> None:
    """List board memberships."""
    _, _client, _check_resp = _cli_helpers()
    with _client() as c:
        data = _check_resp(c.get("/admin/memberships"))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("set-membership")
def admin_set_membership(
    board_slug: str = typer.Argument(...),
    principal_handle: str = typer.Argument(...),
    role: str = typer.Option("agent", "--role"),
) -> None:
    """Assign or update a principal's role on a board."""
    _, _client, _check_resp = _cli_helpers()
    with _client() as c:
        data = _check_resp(
            c.post(
                "/admin/memberships",
                json={"board_slug": board_slug, "principal_handle": principal_handle, "role": role},
            )
        )
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("remove-membership")
def admin_remove_membership(
    board_slug: str = typer.Argument(...),
    principal_handle: str = typer.Argument(...),
) -> None:
    """Remove a principal's membership from a board."""
    _, _client, _check_resp = _cli_helpers()
    with _client() as c:
        data = _check_resp(c.delete(f"/admin/memberships/{board_slug}/{principal_handle}"))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("tokens")
def admin_tokens() -> None:
    """List API tokens (prefixes and scopes)."""
    _, _client, _check_resp = _cli_helpers()
    with _client() as c:
        data = _check_resp(c.get("/admin/tokens"))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("create-token")
def admin_create_token(
    principal_handle: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    scope: list[str] = typer.Option(["*"], "--scope"),
    max_role: str = typer.Option("agent", "--max-role"),
    expires_at: str | None = typer.Option(None, "--expires-at"),
) -> None:
    """Mint a new bearer token for a principal."""
    _, _client, _check_resp = _cli_helpers()
    payload: dict[str, Any] = {
        "principal_handle": principal_handle,
        "name": name,
        "board_scope": scope,
        "max_role": max_role,
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at
    with _client() as c:
        data = _check_resp(c.post("/admin/tokens", json=payload))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("revoke-token")
def admin_revoke_token(token_id: str = typer.Argument(...)) -> None:
    """Revoke an active bearer token by ID."""
    _, _client, _check_resp = _cli_helpers()
    with _client() as c:
        data = _check_resp(c.post(f"/admin/tokens/{token_id}/revoke"))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("bind-antigravity")
def admin_bind_antigravity(
    handle: str = typer.Argument(...),
    board: str = typer.Option(
        "*", "--board", "-b", help="Board slug to route, or '*' (default) for all boards this agent belongs to."
    ),
    conversation: str = typer.Option(..., "--conversation", "-c", help="Existing Antigravity conversation ID."),
    mentions_only: bool = typer.Option(
        False, "--mentions-only", help="Only wake on direct @mentions, @<tag>, or @all."
    ),
    webhook_url: str | None = typer.Option(None, "--webhook-url", help="Override webhook URL from webhook.toml."),
) -> None:
    """Bind an Antigravity conversation ID to wildcard '*' (default) or a board and configure push webhooks."""
    _, _client, _check_resp = _cli_helpers()
    with _client() as c:
        res = bind_antigravity_conversation(
            client=c,
            handle=handle,
            board_slug=board,
            conversation_id=conversation,
            mentions_only=mentions_only,
            webhook_url_override=webhook_url,
        )
    typer.echo(json.dumps(res, indent=2))


@admin_cli.command("onboard")
def admin_onboard(
    handle: str = typer.Argument(...),
    board: list[str] = typer.Option(..., "--board", "-b", help="Board slug(s) to grant access to."),
    target: str = typer.Option("remote", "--target", "-t", help="Target runtime: remote, antigravity, or local."),
    name: str | None = typer.Option(None, "--name", help="Display name if creating principal."),
    role: str = typer.Option("agent", "--role", help="Role: agent, readonly, or owner."),
    tag: list[str] = typer.Option([], "--tag", "-T", help="Capability/group tag(s) (e.g. dev, qa)."),
    conversation: str | None = typer.Option(
        None,
        "--conversation",
        "-c",
        help="Optional Antigravity conversation ID to bind for push wakes (used with --target antigravity).",
    ),
    webhook_url: str | None = typer.Option(None, "--webhook-url", help="Optional routine/webhook URL."),
    webhook_auth: str | None = typer.Option(None, "--webhook-auth", help="Optional webhook Authorization header."),
    skip_plain_notes: bool = typer.Option(
        True, "--skip-notes/--all-messages", help="Skip plain unmentioned notes on webhook."
    ),
    register_mcp: bool = typer.Option(
        False,
        "--register-mcp/--no-register-mcp",
        help="Also register agent-board-<handle> in ~/.gemini/config/mcp_config.json (default off; prefer CLI).",
    ),
    rotate: bool = typer.Option(True, "--rotate/--keep-tokens", help="Revoke prior active tokens for this handle."),
    strict: bool = typer.Option(True, "--strict/--no-strict", help="Remove memberships outside --board."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Create/update an agent, scope boards, rotate token, optionally test webhook, and configure local runtime."""
    _load_config, _client, _check_resp = _cli_helpers()
    clean_target = target.strip().lower()
    if clean_target not in ("remote", "antigravity", "local"):
        typer.echo("Error: --target must be remote, antigravity, or local", err=True)
        raise typer.Exit(1)

    clean_boards = [s.strip() for raw in board for s in raw.split(",") if s.strip()]
    base_url, _ = _load_config()

    payload: dict[str, Any] = {
        "handle": handle,
        "boards": clean_boards,
        "role": role,
        "strict_scope": strict,
        "rotate_tokens": rotate,
        "skip_plain_notes": skip_plain_notes,
        "test_webhook": bool(webhook_url),
    }
    if name is not None:
        payload["display_name"] = name
    if tag:
        payload["tags"] = [s.strip() for raw in tag for s in raw.split(",") if s.strip()]
    if webhook_url is not None:
        payload["webhook_url"] = webhook_url
    if webhook_auth is not None:
        payload["webhook_auth_header"] = webhook_auth

    with _client() as c:
        data = _check_resp(c.post("/admin/onboard", json=payload))
        if clean_target == "antigravity" and conversation:
            data["antigravity_binding"] = bind_antigravity_conversation(
                client=c,
                handle=data["principal_handle"],
                board_slug="*",
                conversation_id=conversation,
                webhook_url_override=webhook_url,
            )

    local_info: dict[str, str] | None = None
    if clean_target in ("antigravity", "local"):
        new_token = data["token"]["token"]
        local_info = install_local_agent_profile(
            data["principal_handle"],
            base_url,
            new_token,
            register_antigravity_mcp=(clean_target == "antigravity" and register_mcp),
        )
        if register_mcp:
            from agent_board.services.onboarding import build_local_profile_onboarding_md

            data["local_profile_onboarding_md"] = build_local_profile_onboarding_md(
                handle=data["principal_handle"],
                boards=clean_boards,
                role=role,
                mcp_server_name=local_info.get("mcp_server_name"),
                cli_cmd=local_info["cli_cmd"],
            )
        me_resp = httpx.get(
            f"{base_url}/api/v1/me",
            headers={"Authorization": f"Bearer {new_token}"},
            timeout=15.0,
        )
        me_data = _check_resp(me_resp)
        if me_data.get("principal", {}).get("handle") != data["principal_handle"]:
            typer.echo("Error: newly installed profile failed identity check", err=True)
            raise typer.Exit(1)
        data["local_profile"] = local_info

    if json_out:
        typer.echo(json.dumps(data, indent=2))
        return

    typer.echo(f"Onboarded @{data['principal_handle']} -> boards={data['boards']} role={data['role']}")
    if data["revoked_prefixes"]:
        typer.echo(f"Revoked prior tokens: {', '.join(data['revoked_prefixes'])}")

    if local_info is not None:
        typer.echo(f"Profile saved: {local_info['profile_path']}")
        typer.echo(f"CLI wrapper:   {local_info['wrapper_path']}")
        if "mcp_server_name" in local_info:
            typer.echo(f"Antigravity MCP added: {local_info['mcp_server_name']} in {local_info['mcp_config_path']}")
        typer.echo("")
        typer.echo("--- Onboarding Prompt ---")
        typer.echo(data["local_profile_onboarding_md"])
    else:
        typer.echo(f"{data['env_var_name']}={data['token']['token']}")
        if data.get("webhook_test"):
            wt = data["webhook_test"]
            status_str = f"OK (HTTP {wt['status_code']})" if wt.get("ok") else f"FAILED ({wt.get('error')})"
            typer.echo(f"Webhook test: {status_str}")
        typer.echo("")
        typer.echo("--- Onboarding Prompt ---")
        typer.echo(data["remote_onboarding_md"])
