"""Server administration CLI (`agent-board`)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import typer
import uvicorn

from agent_board.backup.manager import run_backup, run_restore_drill
from agent_board.config import load_settings
from agent_board.services import auth as auth_svc
from agent_board.services import boards as boards_svc
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.seed import ensure_owner_and_ops, seed_demo_workspace, seed_from_spec
from agent_board.store import principals as principals_store

admin_app = typer.Typer(help="Agent Board server administration CLI.")
principal_app = typer.Typer(help="Manage principals.")
token_app = typer.Typer(help="Manage API tokens.")
board_admin_app = typer.Typer(help="Manage boards.")
member_app = typer.Typer(help="Manage board memberships.")

admin_app.add_typer(principal_app, name="principal")
admin_app.add_typer(token_app, name="token")
admin_app.add_typer(board_admin_app, name="board")
admin_app.add_typer(member_app, name="member")


def _services() -> AppServices:
    return AppServices.create(load_settings())


def _owner_ctx(services: AppServices) -> AuthContext:
    row = principals_store.get_first_server_owner(services.db.read_conn())
    if row is None:
        typer.echo("Server is not initialized. Run 'agent-board init' first.", err=True)
        raise typer.Exit(1)
    return AuthContext(
        principal_id=row["id"],
        handle=row["handle"],
        kind=row["kind"],
        display_name=row["display_name"],
        is_server_owner=bool(row["is_server_owner"]),
        auth_kind="system",
        board_scope=("*",),
        max_role="owner",
    )


@admin_app.command()
def init(
    handle: str = typer.Option("owner", "--handle", help="Owner principal handle."),
    password: str | None = typer.Option(None, "--password", help="Owner password (prompted if omitted)."),
) -> None:
    """Initialize SQLite database, run migrations, and create the server owner + ops board."""
    clean_handle = handle.strip().lower() or "owner"
    if password is None:
        password = typer.prompt(f"Password for {clean_handle}", hide_input=True, confirmation_prompt=True)
    services = _services()
    try:
        res = ensure_owner_and_ops(services, owner_handle=clean_handle, owner_password=password)
        typer.echo(f"Initialized Agent Board at {services.settings.db_path} (owner_created={res['owner_created']})")
    finally:
        services.close()


@admin_app.command("seed")
def seed(
    file: Path | None = typer.Option(None, "--file", help="Path to a JSON seed specification file."),
    demo: bool = typer.Option(False, "--demo", is_flag=True, help="Seed the built-in generic example workspace."),
) -> None:
    """Seed principals, boards, memberships, and initial records from JSON or --demo."""
    if not demo and file is None:
        typer.echo("Pass --file <path.json> or --demo.", err=True)
        raise typer.Exit(1)
    services = _services()
    try:
        if file is not None:
            spec = json.loads(file.read_text(encoding="utf-8"))
            res = seed_from_spec(services, spec)
        else:
            res = seed_demo_workspace(services)
        typer.echo(json.dumps(res, indent=2))
    finally:
        services.close()


@principal_app.command("list")
def principal_list() -> None:
    """List all principals."""
    services = _services()
    try:
        ctx = _owner_ctx(services)
        res = auth_svc.list_principals(services, ctx)
        typer.echo(json.dumps(res, indent=2))
    finally:
        services.close()


@principal_app.command("create")
def principal_create(
    handle: str = typer.Argument(...),
    kind: str = typer.Option("agent", "--kind", help="human or agent"),
    display_name: str = typer.Option("", "--display-name"),
    owner: bool = typer.Option(False, "--owner", is_flag=True),
    password: str | None = typer.Option(None, "--password"),
) -> None:
    """Create a principal."""
    services = _services()
    try:
        ctx = _owner_ctx(services)
        res = auth_svc.create_principal(
            services,
            ctx,
            handle=handle,
            kind=kind,
            display_name=display_name or handle,
            is_server_owner=owner,
            password=password,
        )
        typer.echo(json.dumps(res, indent=2))
    finally:
        services.close()


@token_app.command("create")
def token_create(
    principal: str = typer.Option(..., "--principal"),
    boards: str = typer.Option("*", "--boards", help="Comma-separated board slugs or '*'"),
    role: str = typer.Option("agent", "--role", help="owner, agent, or readonly"),
    name: str = typer.Option("", "--name"),
    expires_at: str | None = typer.Option(None, "--expires-at"),
) -> None:
    """Create an API token (plaintext shown once)."""
    services = _services()
    try:
        ctx = _owner_ctx(services)
        scope = [b.strip() for b in boards.split(",") if b.strip()]
        res = auth_svc.create_token(
            services,
            ctx,
            principal_handle=principal,
            name=name or f"{principal}-token",
            board_scope=scope,
            max_role=role,
            expires_at=expires_at,
        )
        typer.echo(json.dumps(res, indent=2))
    finally:
        services.close()


@token_app.command("revoke")
def token_revoke(token_id: str = typer.Argument(...)) -> None:
    """Revoke an API token by id."""
    services = _services()
    try:
        ctx = _owner_ctx(services)
        res = auth_svc.revoke_token(services, ctx, token_id=token_id)
        typer.echo(json.dumps(res, indent=2))
    finally:
        services.close()


@board_admin_app.command("list")
def board_list() -> None:
    """List all boards."""
    services = _services()
    try:
        ctx = _owner_ctx(services)
        res = boards_svc.list_boards(services, ctx, include_archived=True)
        typer.echo(json.dumps(res, indent=2))
    finally:
        services.close()


@board_admin_app.command("create")
def board_create(
    slug: str = typer.Argument(..., help="URL-safe lowercase board identifier (e.g. my-project)."),
    name: str = typer.Option(..., "--name", help="Human-readable board name."),
    purpose: str = typer.Option(..., "--purpose", help="1-3 sentence board summary shown in the Brief."),
) -> None:
    """Create a new project board (automatically creates 'general' and 'decisions' threads)."""
    services = _services()
    try:
        ctx = _owner_ctx(services)
        res = boards_svc.create_board(services, ctx, slug=slug, name=name, purpose=purpose)
        typer.echo(json.dumps(res, indent=2))
    finally:
        services.close()


@member_app.command("add")
def member_add(
    board: str = typer.Argument(...),
    handle: str = typer.Argument(...),
    role: str = typer.Option("agent", "--role"),
) -> None:
    """Add or update a principal's membership role on a board."""
    services = _services()
    try:
        ctx = _owner_ctx(services)
        res = auth_svc.set_membership(services, ctx, board_slug=board, principal_handle=handle, role=role)
        typer.echo(json.dumps(res, indent=2))
    finally:
        services.close()


@admin_app.command()
def backup() -> None:
    """Run an immediate SQLite online backup + attachment snapshot."""
    services = _services()
    try:
        res = run_backup(services)
        typer.echo(json.dumps(res, indent=2))
        if not res["ok"]:
            raise typer.Exit(1)
    finally:
        services.close()


@admin_app.command("restore-test")
def restore_test(
    backup_path: Path | None = typer.Option(None, "--backup-path", help="Specific .db backup file to verify."),
) -> None:
    """Verify backup integrity and row counts in an isolated temporary directory."""
    if backup_path is not None:
        report = run_restore_drill(backup_path)
        typer.echo(json.dumps(report, indent=2))
        if not report["integrity_ok"] or report["foreign_key_errors"] != 0:
            raise typer.Exit(1)
        return

    settings = load_settings()
    existing = sorted(settings.backups_dir.glob("board-*.db"), reverse=True)
    if existing and settings.db_path.exists():
        report = run_restore_drill(existing[0])
        typer.echo(json.dumps(report, indent=2))
        if not report["integrity_ok"] or report["foreign_key_errors"] != 0:
            raise typer.Exit(1)
        return

    # Self-contained CI drill when no existing backup/data directory is populated yet
    with tempfile.TemporaryDirectory(prefix="agent-board-ci-drill-") as tmp:
        tmp_settings = load_settings(data_dir=Path(tmp) / "data")
        services = AppServices.create(tmp_settings)
        try:
            seed_demo_workspace(services)
            b_res = run_backup(services)
            assert b_res["ok"] and b_res["db_backup_path"]
            report = run_restore_drill(Path(b_res["db_backup_path"]))
            typer.echo(json.dumps(report, indent=2))
            if not report["integrity_ok"] or report["foreign_key_errors"] != 0:
                sys.exit(1)
        finally:
            services.close()


@admin_app.command()
def serve(
    bind: str | None = typer.Option(None, "--bind", help="host:port override"),
    reload: bool = typer.Option(False, "--reload", is_flag=True),
) -> None:
    """Run the Agent Board HTTP + MCP + Web UI server."""
    settings = load_settings()
    if bind:
        if ":" in bind:
            host, _, port_str = bind.rpartition(":")
            host = host.strip("[]") or "127.0.0.1"
            port = int(port_str) if port_str.isdigit() else settings.bind_port
        else:
            host = bind.strip("[]") or "127.0.0.1"
            port = settings.bind_port
    else:
        host = settings.bind_host
        port = settings.bind_port

    if reload:
        uvicorn.run(
            "agent_board.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            proxy_headers=False,
        )
    else:
        from agent_board.app import create_app

        services = AppServices.create(settings)
        try:
            app = create_app(services)
            uvicorn.run(app, host=host, port=port, proxy_headers=False)
        finally:
            services.close()


@admin_app.command("admin-bot")
def admin_bot_cmd(
    config: Path = typer.Option(..., "--config", help="Path to board-admin TOML config file."),
    once: bool = typer.Option(False, "--once", help="Process unread mentions once and exit."),
) -> None:
    """Run the deterministic allowlisted @board-admin daemon."""
    from agent_board.admin_bot import load_admin_bot_config, run_admin_bot, run_once

    cfg = load_admin_bot_config(config)
    if once:
        processed = run_once(cfg)
        typer.echo(f"Processed {processed} message(s).")
        return
    raise SystemExit(run_admin_bot(cfg))


@admin_app.command("claude-channel")
def claude_channel_cmd(
    handle: str = typer.Option(..., "--handle", help="Agent handle receiving metadata-only wakes."),
    config_dir: Path | None = typer.Option(
        None,
        "--config-dir",
        help="Optional Agent Board config directory override (defaults to ~/.config/agent-board).",
    ),
) -> None:
    """Run a stdio Claude Code MCP Channel listener backed by a 0600 Unix domain socket."""
    from agent_board.claude_channel import run_claude_channel

    raise SystemExit(run_claude_channel(handle, config_dir=config_dir))
