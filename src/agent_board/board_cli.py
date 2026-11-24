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


def _format_message_line(m: dict[str, Any]) -> str:
    to_list = m.get("to") if "to" in m else (m.get("mentions") or [])
    cc_list = m.get("cc") or []
    env_parts: list[str] = []
    if to_list:
        env_parts.append("to: " + ", ".join(f"@{h}" for h in to_list))
    if cc_list:
        env_parts.append("cc: " + ", ".join(f"@{h}" for h in cc_list))
    env_suffix = f" [{' | '.join(env_parts)}]" if env_parts else ""
    urgent_tag = "[URGENT] " if m.get("priority") == "urgent" else ""
    return (
        f"#{m['seq']} {urgent_tag}[{m['type']}] @{m['author_handle']} "
        f"({m['thread_title']}){env_suffix}: {m['body_md']}"
    )


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
        typer.echo(_format_message_line(m))


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
                typer.echo(_format_message_line(m))
            since = data["next_since_seq"]
            time.sleep(interval)


@client_app.command()
def post(
    slug: str = typer.Argument(...),
    message: str = typer.Option(..., "-m", "--message"),
    thread: str = typer.Option("general", "--thread"),
    msg_type: str = typer.Option("note", "--type"),
    to: str | None = typer.Option(None, "--to", help="Comma-separated recipients to wake (pass '' for none)."),
    cc: str | None = typer.Option(None, "--cc", help="Comma-separated FYI recipients (never wakes)."),
    silent: bool = typer.Option(False, "--silent", help="Set to=[] so no recipients wake."),
    urgent: bool = typer.Option(False, "--urgent", help="Mark urgent (P0); bypasses recipient DND."),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
) -> None:
    """Post a message to a board."""
    parsed_to = [x.strip() for x in to.split(",") if x.strip()] if to is not None else None
    if silent:
        if parsed_to:
            raise typer.BadParameter("Cannot combine --silent with non-empty --to")
        if urgent:
            raise typer.BadParameter("Cannot combine --urgent with --silent")
        parsed_to = []
    payload: dict[str, Any] = {
        "thread_title": thread,
        "type": msg_type,
        "priority": "urgent" if urgent else "normal",
        "body_md": message,
    }
    if parsed_to is not None:
        payload["to"] = parsed_to
    if cc is not None:
        payload["cc"] = [x.strip() for x in cc.split(",") if x.strip()]

    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    with _client() as c:
        data = _check_resp(
            c.post(
                f"/boards/{slug}/messages",
                json=payload,
                headers=headers,
            )
        )
    typer.echo(f"Posted #{data['seq']} ({data['id']})")


@client_app.command()
def dnd(
    minutes: int = typer.Option(30, "--minutes", "-m", help="Minutes to hold non-urgent wakes (1-240)."),
    reason: str | None = typer.Option(None, "--reason", "-r", help="Optional reason shown to teammates."),
    off: bool = typer.Option(False, "--off", help="Clear DND and expedite deferred wakes."),
    status: bool = typer.Option(False, "--status", help="Show current DND status without changing it."),
) -> None:
    """Set, inspect, or clear Do-Not-Disturb wake deferral for the current profile."""
    with _client() as c:
        if status:
            me_data = _check_resp(c.get("/me"))
            dnd_info = me_data.get("principal", {}).get("dnd") or {"active": False, "until": None, "reason": None}
            typer.echo(json.dumps(dnd_info, indent=2))
            return
        payload: dict[str, Any] = {"enabled": not off}
        if not off:
            payload["duration_minutes"] = minutes
            if reason is not None:
                payload["reason"] = reason
        res = _check_resp(c.post("/me/dnd", json=payload))
    typer.echo(json.dumps(res, indent=2))


@client_app.command()
def ack(slug: str = typer.Argument(...), read_seq: int = typer.Argument(...)) -> None:
    """Advance read cursor to read_seq."""
    with _client() as c:
        data = _check_resp(c.post(f"/boards/{slug}/ack", json={"read_seq": read_seq}))
    typer.echo(json.dumps(data))


@client_app.command()
def remind(
    slug: str = typer.Argument(...),
    message: str = typer.Option(..., "--message", "-m", help="Follow-up reminder text."),
    delay_minutes: int | None = typer.Option(
        None, "--in", "-i", help="Minutes from now to fire (1-10080). Mutually exclusive with --at."
    ),
    due_at: str | None = typer.Option(
        None, "--at", help="UTC ISO-8601 timestamp to fire (e.g. 2026-11-25T15:00:00Z)."
    ),
    thread: str | None = typer.Option(None, "--thread", "-t", help="Optional thread ID or title."),
    cancel_on_reply: bool = typer.Option(
        False,
        "--cancel-on-reply",
        help="Auto-cancel if someone else replies in --thread before due time.",
    ),
) -> None:
    """Schedule a durable follow-up timer that wakes you on the board when due."""
    payload: dict[str, Any] = {
        "message": message,
        "cancel_on_reply": cancel_on_reply,
    }
    if delay_minutes is not None:
        payload["delay_minutes"] = delay_minutes
    if due_at is not None:
        payload["due_at"] = due_at
    if thread is not None:
        payload["thread_id"] = thread
    with _client() as c:
        data = _check_resp(c.post(f"/boards/{slug}/timers", json=payload))
    typer.echo(json.dumps(data, indent=2))


@client_app.command()
def reminders(
    slug: str = typer.Argument(...),
    status: str = typer.Option("pending", "--status", help="Filter: pending, fired, cancelled, or all."),
) -> None:
    """List your follow-up timers on a board."""
    with _client() as c:
        data = _check_resp(c.get(f"/boards/{slug}/timers", params={"status": status}))
    for t in data.get("timers", []):
        thr = f" [{t['thread_title']}]" if t.get("thread_title") else ""
        cor = " (cancel-on-reply)" if t.get("cancel_on_reply") else ""
        typer.echo(f"{t['id']} [{t['status']}] due={t['due_at']}{thr}{cor}: {t['message']}")


@client_app.command()
def unremind(slug: str = typer.Argument(...), timer_id: str = typer.Argument(...)) -> None:
    """Cancel a pending follow-up timer."""
    with _client() as c:
        data = _check_resp(c.delete(f"/boards/{slug}/timers/{timer_id}"))
    typer.echo(json.dumps(data))


@client_app.command()
def handoff(
    slug: str = typer.Argument(...),
    thread: str = typer.Argument(..., help="Thread ID or title."),
    message: str | None = typer.Option(None, "--message", "-m", help="Pinned progress/handoff summary."),
    clear: bool = typer.Option(False, "--clear", help="Clear the thread's pinned handoff summary."),
) -> None:
    """Set or clear a pinned thread handoff summary surfaced in `board brief`."""
    if not clear and not (message and message.strip()):
        typer.echo("Provide --message/-m or --clear", err=True)
        raise typer.Exit(1)
    from urllib.parse import quote

    encoded_thread = quote(thread, safe="")
    with _client() as c:
        data = _check_resp(
            c.put(
                f"/boards/{slug}/threads/{encoded_thread}/handoff",
                json={"handoff_md": None if clear else message},
            )
        )
    typer.echo(json.dumps({"thread_id": data["id"], "title": data["title"], "handoff_md": data.get("handoff_md")}))


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


@client_app.command("telemetry")
def telemetry(
    brain_dir: Path = typer.Option(
        Path("~/.gemini/antigravity/brain").expanduser(),
        "--brain-dir",
        help="Path to Antigravity brain directory.",
    ),
    active_conversations: Path = typer.Option(
        Path("~/.config/agent-board/active_conversations.json").expanduser(),
        "--active-conversations",
        help="Path to active_conversations.json mapping UUIDs to handles.",
    ),
    host_id: str | None = typer.Option(None, "--host-id", help="Override reported host identifier."),
    push: bool = typer.Option(False, "--push", help="Upload sanitized metrics snapshot to /api/v1/admin/telemetry."),
    alert_board: str | None = typer.Option(
        None, "--alert-board", help="Optional board slug to notify on threshold breach (e.g. ops)."
    ),
    alert_to: str = typer.Option("tool-master", "--alert-to", help="Recipient handle for telemetry alerts."),
    cooldown_hours: float = typer.Option(
        4.0, "--cooldown-hours", help="Minimum hours between repeat alerts for the same condition."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print full JSON snapshot."),
) -> None:
    """Scan Antigravity transcript logs with zero LLM calls and optionally push / alert."""
    from agent_board.telemetry import collect_antigravity_telemetry, dispatch_telemetry_alerts

    snap = collect_antigravity_telemetry(
        brain_dir=brain_dir.expanduser(),
        active_conversations_path=active_conversations.expanduser(),
        host_id=host_id,
    )
    dispatched: list[dict[str, Any]] = []
    if push or alert_board:
        with _client() as c:
            if push:
                _check_resp(c.post("/admin/telemetry", json=snap))
            if alert_board:
                state_path = Path("~/.config/agent-board/telemetry_alert_state.json").expanduser()
                dispatched = dispatch_telemetry_alerts(
                    snap,
                    client=c,
                    alert_board=alert_board,
                    alert_to=alert_to,
                    state_path=state_path,
                    cooldown_hours=cooldown_hours,
                )

    if json_out:
        out = {**snap, "alerts_dispatched": len(dispatched)}
        typer.echo(json.dumps(out, indent=2))
        return

    g = snap["global"]
    hit_str = f"{g['hit_rate_5h']}%" if g["hit_rate_5h"] is not None else "—"
    iw_tot = int(g.get("idle_wake_total_5h", 0))
    iw_rate = g.get("idle_wake_miss_rate_5h")
    iw_str = (
        f"{iw_rate}% ({g.get('idle_wake_partial_5h', 0)}p+{g.get('idle_wake_zero_5h', 0)}z/{iw_tot})"
        if iw_tot > 0 and iw_rate is not None
        else "—"
    )
    typer.echo(
        f"Host {snap['host_id']} · 5h uncached={g['uncached_5h']:,} · hit={hit_str} · "
        f"partial_collapses={g['partial_collapses_5h']} · idle_wake_miss={iw_str} · "
        f"sessions={g['sessions_scanned']}"
    )
    for handle, info in sorted(snap["agents"].items()):
        ah = f"{info['hit_rate_5h']}%" if info["hit_rate_5h"] is not None else "—"
        typer.echo(
            f"  @{handle:14} ctx={info['live_context_tokens'] // 1000:4d}k  "
            f"5h_uncached={info['uncached_5h']:9,}  hit={ah:>6}  "
            f"webhook_5h={info['webhook_uncached_5h']:9,}  collapses={info['partial_collapses_5h']}"
        )
    if snap["alerts"]:
        typer.echo(f"Alerts ({len(snap['alerts'])} active, {len(dispatched)} posted):")
        for a in snap["alerts"]:
            typer.echo(f"  [{a['severity'].upper()}] {a['message']}")


@client_app.command("sidecar")
def sidecar(
    test_once: bool = typer.Option(False, "--test-once", help="Verify one valid webhook request and exit 0."),
) -> None:
    """Run the local workstation Antigravity webhook sidecar listener."""
    from agent_board.sidecar import main as sidecar_main

    argv = ["--test-once"] if test_once else []
    raise SystemExit(sidecar_main(argv))


client_app.add_typer(admin_cli, name="admin")
