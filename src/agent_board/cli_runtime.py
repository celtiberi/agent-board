"""Local workstation runtime helpers for agent profiles, wrappers, and Antigravity webhook binding."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

import httpx
import typer

from agent_board.constants import ACTIONABLE_MESSAGE_TYPES


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
        mcp_data = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.exists() else {}
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


def bind_antigravity_conversation(
    *,
    client: httpx.Client,
    handle: str,
    conversation_id: str,
    board_slug: str = "*",
    mentions_only: bool = False,
    webhook_url_override: str | None = None,
) -> dict[str, Any]:
    """Bind an Antigravity conversation ID to wildcard '*' or a specific board and configure push webhooks."""
    from agent_board.board_cli import _check_resp

    clean_handle = handle.strip().lower()
    clean_board = board_slug.strip().lower() or "*"
    clean_conv = conversation_id.strip()
    if not clean_conv:
        typer.echo("Error: conversation ID is required", err=True)
        raise typer.Exit(1)

    cfg_dir = Path("~/.config/agent-board").expanduser()
    hook_path = cfg_dir / "webhook.toml"
    if not hook_path.exists():
        typer.echo(f"Missing {hook_path}: configure the local webhook listener first.", err=True)
        raise typer.Exit(1)

    hook_cfg = tomllib.loads(hook_path.read_text(encoding="utf-8"))
    bearer_token = str(hook_cfg.get("bearer_token", "")).strip()
    hmac_secret = str(hook_cfg.get("hmac_secret", "")).strip()
    target_url = (webhook_url_override or str(hook_cfg.get("webhook_url", ""))).strip()
    if not target_url:
        typer.echo(f"Missing webhook_url in {hook_path} and --webhook-url was not provided.", err=True)
        raise typer.Exit(1)

    active_path = cfg_dir / "active_conversations.json"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, Any] = {}
    if active_path.exists():
        mapping = json.loads(active_path.read_text(encoding="utf-8"))

    sub_to_conv: dict[str, str] = {}
    for raw_key, entry in mapping.items():
        if not isinstance(entry, dict):
            continue
        key_handle = raw_key.split(":", 1)[1].strip().lower() if ":" in raw_key else "admin"
        resolved_handle = str(entry.get("handle", "")).strip().lower() or key_handle
        sub_id = str(entry.get("subscription_id", "")).strip()
        cid = str(entry.get("conversation_id", "")).strip()
        if resolved_handle == clean_handle and sub_id and cid:
            sub_to_conv[sub_id] = cid

    subs_data = _check_resp(client.get("/subscriptions", params={"all": "true"}))
    deleted_sub_ids: set[str] = set()
    for s in subs_data.get("subscriptions", []):
        if s.get("principal_handle") != clean_handle:
            continue
        s_boards = [str(b).strip().lower() for b in (s.get("boards") or [])]
        s_flt = s.get("filter") if isinstance(s.get("filter"), dict) else {}
        s_mentions_only = bool(s_flt.get("mentions_only", False))
        sid = str(s.get("id", ""))
        should_delete = (
            (
                "*" in s_boards
                or (
                    str(s.get("url", "")).strip() == target_url
                    and s_mentions_only == mentions_only
                    and sub_to_conv.get(sid) == clean_conv
                )
            )
            if clean_board == "*"
            else clean_board in s_boards
        )
        if should_delete:
            _check_resp(client.delete(f"/subscriptions/{sid}"))
            if sid:
                deleted_sub_ids.add(sid)

    sub_filter: dict[str, Any] = {"exclude_self": True, "mentions_only": mentions_only}
    if not mentions_only:
        sub_filter["types"] = list(ACTIONABLE_MESSAGE_TYPES)

    sub_data = _check_resp(
        client.post(
            "/subscriptions",
            json={
                "principal_handle": clean_handle,
                "url": target_url,
                "boards": [clean_board],
                "filter": sub_filter,
                "auth_header": f"Bearer {bearer_token}",
                "hmac_secret": hmac_secret,
                "min_interval_s": 30,
                "debounce_s": 5,
            },
        )
    )
    test_res = _check_resp(client.post(f"/subscriptions/{sub_data['id']}/test"))

    if clean_board == "*":
        stale_keys = [
            k
            for k, entry in mapping.items()
            if isinstance(entry, dict) and str(entry.get("subscription_id", "")).strip() in deleted_sub_ids
        ]
        for k in stale_keys:
            mapping.pop(k, None)
    else:
        legacy_entry = mapping.pop(clean_board, None)
        if isinstance(legacy_entry, dict):
            legacy_handle = str(legacy_entry.get("handle", "")).strip().lower() or "admin"
            legacy_cid = str(legacy_entry.get("conversation_id", "")).strip()
            if legacy_handle != clean_handle and legacy_cid:
                mapping.setdefault(
                    f"{clean_board}:{legacy_handle}",
                    {
                        "board": clean_board,
                        "handle": legacy_handle,
                        "conversation_id": legacy_cid,
                        "subscription_id": str(legacy_entry.get("subscription_id", "")).strip(),
                    },
                )
        elif isinstance(legacy_entry, str) and legacy_entry.strip() and clean_handle != "admin":
            mapping.setdefault(
                f"{clean_board}:admin",
                {
                    "board": clean_board,
                    "handle": "admin",
                    "conversation_id": legacy_entry.strip(),
                    "subscription_id": "",
                },
            )

    route_key = f"{clean_board}:{clean_handle}"
    mapping[route_key] = {
        "board": clean_board,
        "handle": clean_handle,
        "conversation_id": clean_conv,
        "subscription_id": sub_data["id"],
    }
    active_path.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    os.chmod(active_path, 0o600)

    return {
        "board": clean_board,
        "handle": clean_handle,
        "route_key": route_key,
        "conversation_id": clean_conv,
        "subscription_id": sub_data["id"],
        "mentions_only": mentions_only,
        "webhook_url": target_url,
        "webhook_test": test_res,
        "active_conversations_path": str(active_path),
    }
