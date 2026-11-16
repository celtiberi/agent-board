"""Thin REST client CLI (`board`) for human operators and local scripts."""

from __future__ import annotations

import json
import os
import shutil
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx
import typer

client_app = typer.Typer(help="Agent Board CLI client.")
_ACTIVE_PROFILE: str | None = None


@client_app.callback()
def _main_callback(
    profile: str | None = typer.Option(
        None,
        "--profile",
        "-p",
        help="Named client profile (~/.config/agent-board/profiles/<name>.toml).",
    ),
) -> None:
    global _ACTIVE_PROFILE
    _ACTIVE_PROFILE = profile.strip() if profile else None


def _load_config(profile_override: str | None = None) -> tuple[str, str]:
    cfg_dir = Path("~/.config/agent-board").expanduser()
    prof = (
        profile_override.strip()
        if profile_override is not None
        else (_ACTIVE_PROFILE or os.environ.get("AGENT_BOARD_PROFILE", "").strip())
    )
    if prof:
        prof_file = cfg_dir / "profiles" / f"{prof}.toml"
        if not prof_file.exists():
            typer.echo(f"Missing profile '{prof}': {prof_file} not found", err=True)
            raise typer.Exit(1)
        data = tomllib.loads(prof_file.read_text(encoding="utf-8"))
        url = str(data.get("url", "")).strip().rstrip("/") or "http://127.0.0.1:8790"
        token = str(data.get("token", "")).strip()
        if not token:
            typer.echo(f"Profile '{prof}' in {prof_file} has no token", err=True)
            raise typer.Exit(1)
        return url, token

    url = os.environ.get("AGENT_BOARD_URL", "").strip()
    token = os.environ.get("AGENT_BOARD_TOKEN", "").strip()

    toml_file = cfg_dir / "config.toml"
    if toml_file.exists():
        data = tomllib.loads(toml_file.read_text(encoding="utf-8"))
        url = url or str(data.get("url", "")).strip()
        token = token or str(data.get("token", "")).strip()

    token_file = cfg_dir / "token"
    if not token and token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()

    url = (url or "http://127.0.0.1:8790").rstrip("/")
    if not token:
        typer.echo(
            "Missing token: set AGENT_BOARD_TOKEN or write ~/.config/agent-board/token",
            err=True,
        )
        raise typer.Exit(1)
    return url, token


def _client() -> httpx.Client:
    url, token = _load_config()
    return httpx.Client(
        base_url=f"{url}/api/v1",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    )


def _check_resp(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        typer.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        raise typer.Exit(1)
    ct = resp.headers.get("content-type", "")
    if "application/json" in ct:
        return resp.json()
    return resp.text


@client_app.command()
def me() -> None:
    """Show the authenticated principal and effective board visibility."""
    with _client() as c:
        data = _check_resp(c.get("/me"))
    typer.echo(json.dumps(data, indent=2))


@client_app.command()
def boards() -> None:
    """List visible boards with unread counts."""
    with _client() as c:
        data = _check_resp(c.get("/boards"))
    for b in data["boards"]:
        typer.echo(
            f"{b['slug']:18} role={b['role'] or '-':8} unread={b['unread_count']} "
            f"issues={b['open_issues_count']} — {b['name']}"
        )


@client_app.command()
def brief(
    slug: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Print the Project Brief for a board."""
    fmt = "json" if json_out else "markdown"
    with _client() as c:
        res = _check_resp(c.get(f"/boards/{slug}/brief", params={"format": fmt}))
    if json_out:
        typer.echo(json.dumps(res, indent=2))
    else:
        typer.echo(res)


@client_app.command()
def read(
    slug: str = typer.Argument(...),
    since_seq: int | None = typer.Option(None, "--since-seq"),
    limit: int = typer.Option(200, "--limit"),
) -> None:
    """Read messages since cursor (or since_seq)."""
    params: dict[str, Any] = {"limit": limit}
    if since_seq is not None:
        params["since_seq"] = since_seq
    with _client() as c:
        data = _check_resp(c.get(f"/boards/{slug}/messages", params=params))
    for m in data["messages"]:
        typer.echo(f"#{m['seq']} [{m['type']}] @{m['author_handle']} ({m['thread_title']}): {m['body_md']}")


@client_app.command()
def tail(
    slug: str = typer.Argument(...),
    interval: float = typer.Option(3.0, "--interval"),
) -> None:
    """Stream new messages on a board until interrupted."""
    with _client() as c:
        cur = _check_resp(c.get(f"/boards/{slug}/brief", params={"format": "json"}))
        since = cur["cursor"]["read_seq"]
        while True:
            data = _check_resp(c.get(f"/boards/{slug}/messages", params={"since_seq": since}))
            for m in data["messages"]:
                typer.echo(f"#{m['seq']} [{m['type']}] @{m['author_handle']} ({m['thread_title']}): {m['body_md']}")
            since = data["next_since_seq"]
            time.sleep(interval)


@client_app.command()
def post(
    slug: str = typer.Argument(...),
    message: str = typer.Option(..., "-m", "--message"),
    thread: str = typer.Option("general", "--thread"),
    msg_type: str = typer.Option("note", "--type"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
) -> None:
    """Post a message to a board."""
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    with _client() as c:
        data = _check_resp(
            c.post(
                f"/boards/{slug}/messages",
                json={"thread_title": thread, "type": msg_type, "body_md": message},
                headers=headers,
            )
        )
    typer.echo(f"Posted #{data['seq']} ({data['id']})")


@client_app.command()
def ack(slug: str = typer.Argument(...), read_seq: int = typer.Argument(...)) -> None:
    """Advance read cursor to read_seq."""
    with _client() as c:
        data = _check_resp(c.post(f"/boards/{slug}/ack", json={"read_seq": read_seq}))
    typer.echo(json.dumps(data))


@client_app.command()
def decide(
    slug: str = typer.Argument(...),
    title: str = typer.Option(..., "--title"),
    statement: str = typer.Option(..., "--statement"),
    by: str = typer.Option("owner", "--by"),
    on: str = typer.Option(..., "--on"),
    rationale: str | None = typer.Option(None, "--rationale"),
) -> None:
    """Record a decision on a board."""
    with _client() as c:
        data = _check_resp(
            c.post(
                f"/boards/{slug}/decisions",
                json={
                    "title": title,
                    "statement": statement,
                    "decided_by": by,
                    "decided_on": on,
                    "rationale": rationale,
                },
            )
        )
    typer.echo(f"Recorded {data['ref']}: {data['title']}")


@client_app.command()
def issues(slug: str = typer.Argument(...), status: str = typer.Option("active", "--status")) -> None:
    """List issues on a board."""
    with _client() as c:
        data = _check_resp(c.get(f"/boards/{slug}/issues", params={"status": status}))
    for i in data["issues"]:
        typer.echo(f"{i['ref']} [{i['severity']}] ({i['status']}) {i['title']}")


@client_app.command()
def search(query: str = typer.Argument(...), board: str | None = typer.Option(None, "--board")) -> None:
    """Search across accessible boards."""
    params: dict[str, Any] = {"q": query}
    if board:
        params["board"] = board
    with _client() as c:
        data = _check_resp(c.get("/search", params=params))
    for h in data["hits"]:
        typer.echo(f"[{h['board_slug']}] {h['kind']} {h['ref'] or ''} — {h['title']}: {h['snippet']}")


async def _run_mcp_stdio(url: str, token: str) -> None:
    import anyio
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.stdio import stdio_server
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.shared.message import SessionMessage

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as hc:
        async with stdio_server() as (stdio_read, stdio_write):
            async with streamable_http_client(f"{url}/mcp", http_client=hc, terminate_on_close=False) as (
                http_read,
                http_write,
            ):
                async with anyio.create_task_group() as tg:

                    async def _client_to_server() -> None:
                        async for item in stdio_read:
                            if isinstance(item, SessionMessage):
                                await http_write.send(item)
                        tg.cancel_scope.cancel()

                    async def _server_to_client() -> None:
                        async for item in http_read:
                            if isinstance(item, SessionMessage):
                                await stdio_write.send(item)

                    tg.start_soon(_client_to_server)
                    tg.start_soon(_server_to_client)


@client_app.command("mcp-stdio")
def mcp_stdio() -> None:
    """Bridge local MCP stdio clients to the remote Streamable HTTP /mcp endpoint."""
    import anyio

    url, token = _load_config()
    anyio.run(_run_mcp_stdio, url, token)


admin_cli = typer.Typer(help="Server owner administration commands.")
client_app.add_typer(admin_cli, name="admin")


@admin_cli.command("create-board")
def admin_create_board(
    slug: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    purpose: str = typer.Option(..., "--purpose"),
) -> None:
    """Create a new board."""
    with _client() as c:
        data = _check_resp(c.post("/boards", json={"slug": slug, "name": name, "purpose": purpose}))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("principals")
def admin_principals() -> None:
    """List principals."""
    with _client() as c:
        data = _check_resp(c.get("/admin/principals"))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("create-principal")
def admin_create_principal(
    handle: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    kind: str = typer.Option("agent", "--kind"),
    owner: bool = typer.Option(False, "--owner/--no-owner"),
) -> None:
    """Create a principal."""
    with _client() as c:
        data = _check_resp(
            c.post(
                "/admin/principals",
                json={"handle": handle, "kind": kind, "display_name": name, "is_server_owner": owner},
            )
        )
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("memberships")
def admin_memberships() -> None:
    """List board memberships."""
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
    with _client() as c:
        data = _check_resp(c.delete(f"/admin/memberships/{board_slug}/{principal_handle}"))
    typer.echo(json.dumps(data, indent=2))


@admin_cli.command("tokens")
def admin_tokens() -> None:
    """List API tokens (prefixes and scopes)."""
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
    with _client() as c:
        data = _check_resp(c.post(f"/admin/tokens/{token_id}/revoke"))
    typer.echo(json.dumps(data, indent=2))


def install_local_agent_profile(
    handle: str,
    url: str,
    token: str,
    *,
    register_antigravity_mcp: bool,
) -> dict[str, str]:
    """Install a named profile, wrapper CLI (~/.local/bin/board-<handle>), and optional Antigravity MCP entry."""
    clean_handle = handle.strip().lower()
    cfg_dir = Path("~/.config/agent-board").expanduser()
    profiles_dir = cfg_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profiles_dir, 0o700)

    profile_path = profiles_dir / f"{clean_handle}.toml"
    profile_path.write_text(
        f'url = "{url.rstrip("/")}"\ntoken = "{token}"\n',
        encoding="utf-8",
    )
    os.chmod(profile_path, 0o600)

    bin_dir = Path("~/.local/bin").expanduser()
    bin_dir.mkdir(parents=True, exist_ok=True)
    board_bin = shutil.which("board") or str(bin_dir / "board")

    wrapper_path = bin_dir / f"board-{clean_handle}"
    wrapper_path.write_text(
        f'#!/bin/sh\nexec "{board_bin}" --profile "{clean_handle}" "$@"\n',
        encoding="utf-8",
    )
    os.chmod(wrapper_path, 0o755)

    out = {
        "profile_path": str(profile_path),
        "wrapper_path": str(wrapper_path),
        "cli_cmd": f"board-{clean_handle}",
    }

    if register_antigravity_mcp:
        mcp_path = Path("~/.gemini/config/mcp_config.json").expanduser()
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        if mcp_path.exists():
            mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
        else:
            mcp_data = {}
        servers = mcp_data.setdefault("mcpServers", {})
        mcp_name = f"agent-board-{clean_handle}"
        servers[mcp_name] = {
            "command": board_bin,
            "args": ["--profile", clean_handle, "mcp-stdio"],
        }
        mcp_path.write_text(json.dumps(mcp_data, indent=2) + "\n", encoding="utf-8")
        os.chmod(mcp_path, 0o600)
        out["mcp_config_path"] = str(mcp_path)
        out["mcp_server_name"] = mcp_name

    return out


@admin_cli.command("onboard")
def admin_onboard(
    handle: str = typer.Argument(...),
    board: list[str] = typer.Option(..., "--board", "-b", help="Board slug(s) to grant access to."),
    target: str = typer.Option("remote", "--target", "-t", help="Target runtime: remote, antigravity, or local."),
    name: str | None = typer.Option(None, "--name", help="Display name if creating principal."),
    role: str = typer.Option("agent", "--role", help="Role: agent, readonly, or owner."),
    webhook_url: str | None = typer.Option(None, "--webhook-url", help="Optional routine/webhook URL."),
    webhook_auth: str | None = typer.Option(None, "--webhook-auth", help="Optional webhook Authorization header."),
    skip_notes: bool = typer.Option(
        True, "--skip-notes/--all-messages", help="Skip plain unmentioned notes on webhook."
    ),
    rotate: bool = typer.Option(True, "--rotate/--keep-tokens", help="Revoke prior active tokens for this handle."),
    strict: bool = typer.Option(True, "--strict/--no-strict", help="Remove memberships outside --board."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Create/update an agent, scope boards, rotate token, optionally test webhook, and configure local runtime."""
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
        "skip_plain_notes": skip_notes,
        "test_webhook": bool(webhook_url),
    }
    if name is not None:
        payload["display_name"] = name
    if webhook_url is not None:
        payload["webhook_url"] = webhook_url
    if webhook_auth is not None:
        payload["webhook_auth_header"] = webhook_auth

    with _client() as c:
        data = _check_resp(c.post("/admin/onboard", json=payload))

    local_info: dict[str, str] | None = None
    if clean_target in ("antigravity", "local"):
        new_token = data["token"]["token"]
        local_info = install_local_agent_profile(
            data["principal_handle"],
            base_url,
            new_token,
            register_antigravity_mcp=(clean_target == "antigravity"),
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
