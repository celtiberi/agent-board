"""Thin REST client CLI (`board`) for human operators and local scripts."""

from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx
import typer

from agent_board.cli_admin import admin_cli
from agent_board.cli_runtime import (
    bind_antigravity_conversation as bind_antigravity_conversation,
)
from agent_board.cli_runtime import (
    install_local_agent_profile as install_local_agent_profile,
)

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


@client_app.command("sidecar")
def sidecar(
    test_once: bool = typer.Option(False, "--test-once", help="Verify one valid webhook request and exit 0."),
) -> None:
    """Run the local workstation Antigravity webhook sidecar listener."""
    from agent_board.sidecar import main as sidecar_main

    argv = ["--test-once"] if test_once else []
    raise SystemExit(sidecar_main(argv))


client_app.add_typer(admin_cli, name="admin")
