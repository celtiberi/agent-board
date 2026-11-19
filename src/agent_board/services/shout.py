"""Server-owner cross-board announcement service."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_board.errors import ValidationError
from agent_board.ids import BROADCAST_MENTIONS, normalize_principal_tags
from agent_board.services.auth import record_audit, require_server_owner
from agent_board.services.context import AppServices, AuthContext
from agent_board.store import boards as boards_store


def shout(
    services: AppServices,
    ctx: AuthContext,
    *,
    body_md: str,
    tag: str | None = None,
    all_boards: bool = False,
    exclude_boards: list[str] | None = None,
    thread_title: str = "general",
    wake: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Server-owner cross-board announcement targeted by principal tag or sent to all active project boards."""
    from agent_board.services.messages import _MENTION_RE, post_message_in_tx

    require_server_owner(ctx)
    clean_body = body_md.strip()
    if not clean_body:
        raise ValidationError("body_md must not be empty")
    if bool(tag and tag.strip()) == bool(all_boards):
        raise ValidationError("Specify exactly one of tag or all_boards=True")

    clean_tag: str | None = None
    if tag and tag.strip():
        try:
            parsed = normalize_principal_tags([tag])
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if not parsed:
            raise ValidationError("Invalid tag")
        clean_tag = parsed[0]

    clean_thread = thread_title.strip() or "general"
    exclude_set = {"ops", *(b.strip().lower() for b in (exclude_boards or []) if b.strip())}

    existing_mentions = set(_MENTION_RE.findall(clean_body.lower()))
    if not wake:
        effective_body = clean_body
        msg_type = "note"
    elif clean_tag is not None:
        effective_body = clean_body if clean_tag in existing_mentions else f"@{clean_tag} {clean_body}"
        msg_type = "status"
    else:
        effective_body = clean_body if (existing_mentions & BROADCAST_MENTIONS) else f"@all {clean_body}"
        msg_type = "status"

    def _collect_targets(conn: sqlite3.Connection, board_row: sqlite3.Row) -> list[str]:
        members = boards_store.list_memberships_for_board(conn, board_row["id"])
        active_members = [
            m
            for m in members
            if m["kind"] != "system" and m["disabled_at"] is None and str(m["principal_id"]) != ctx.principal_id
        ]
        if clean_tag is not None:
            return [
                str(m["handle"])
                for m in active_members
                if clean_tag in json.loads(m["tags"] if "tags" in m.keys() and m["tags"] else "[]")
            ]
        return [str(m["handle"]) for m in active_members]

    deliveries: list[dict[str, Any]] = []
    if dry_run:
        with services.db.read_snapshot() as conn:
            for b in boards_store.list_all_boards(conn):
                slug = str(b["slug"])
                if b["archived_at"] is not None or slug in exclude_set:
                    continue
                targets = _collect_targets(conn, b)
                if not targets:
                    continue
                deliveries.append(
                    {
                        "board": slug,
                        "targets": targets,
                        "would_wake": wake,
                    }
                )
        return {
            "tag": clean_tag,
            "all_boards": all_boards,
            "wake": wake,
            "dry_run": True,
            "thread_title": clean_thread,
            "deliveries": deliveries,
        }

    with services.db.write_tx() as conn:
        for b in boards_store.list_all_boards(conn):
            slug = str(b["slug"])
            if b["archived_at"] is not None or slug in exclude_set:
                continue
            targets = _collect_targets(conn, b)
            if not targets:
                continue
            posted = post_message_in_tx(
                conn,
                services=services,
                ctx=ctx,
                board=b,
                msg_type=msg_type,
                body_md=effective_body,
                thread_title=clean_thread,
                suppress_wake=(not wake),
            )
            deliveries.append(
                {
                    "board": slug,
                    "seq": posted["seq"],
                    "message_id": posted["id"],
                    "targets": targets,
                    "woke": wake,
                }
            )
        record_audit(
            conn,
            ctx=ctx,
            action="admin.shout",
            ok=True,
            target=clean_tag or "all_boards",
            detail={
                "tag": clean_tag,
                "all_boards": all_boards,
                "wake": wake,
                "boards": [d["board"] for d in deliveries],
            },
        )

    return {
        "tag": clean_tag,
        "all_boards": all_boards,
        "wake": wake,
        "dry_run": False,
        "thread_title": clean_thread,
        "deliveries": deliveries,
    }
