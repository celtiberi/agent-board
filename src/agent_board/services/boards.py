"""Domain service for boards, memberships, and threads."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_board.errors import ConflictError, NotFoundError, ValidationError
from agent_board.ids import is_valid_slug, new_ulid, utc_now_iso
from agent_board.services.auth import record_audit, require_board_role, require_server_owner
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.idempotency import execute_idempotent
from agent_board.store import boards as boards_store
from agent_board.store import principals as principals_store

_DEFAULT_SETTINGS = {"agents_may_record_decisions": True}


def _parse_settings(raw_json: str) -> dict[str, Any]:
    parsed = json.loads(raw_json)
    merged = dict(_DEFAULT_SETTINGS)
    merged.update(parsed)
    return merged


def board_row_to_dict(row: sqlite3.Row, ctx: AuthContext) -> dict[str, Any]:
    m_role = row["membership_role"] if "membership_role" in row.keys() else None
    if m_role is None and ctx.is_server_owner:
        m_role = "owner"
    eff_role = ctx.effective_role(m_role) if m_role else None
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "purpose": row["purpose"],
        "settings": _parse_settings(row["settings_json"]),
        "next_seq": int(row["next_seq"]),
        "unread_count": int(row["unread_count"]) if "unread_count" in row.keys() else 0,
        "open_issues_count": (int(row["open_issues_count"]) if "open_issues_count" in row.keys() else 0),
        "active_decisions_count": (int(row["active_decisions_count"]) if "active_decisions_count" in row.keys() else 0),
        "last_activity_at": (row["last_activity_at"] if "last_activity_at" in row.keys() else None),
        "created_at": row["created_at"],
        "archived_at": row["archived_at"],
        "role": eff_role,
    }


def thread_row_to_dict(row: sqlite3.Row, board_slug: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "board_id": row["board_id"],
        "board_slug": board_slug,
        "title": row["title"],
        "created_by": row["created_by"],
        "created_by_handle": row["created_by_handle"],
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
        "message_count": int(row["message_count"]),
        "last_seq": int(row["last_seq"]),
        "last_activity_at": row["last_activity_at"],
    }


def create_board_in_tx(
    conn: sqlite3.Connection,
    *,
    ctx: AuthContext,
    slug: str,
    name: str,
    purpose: str,
    settings: dict[str, Any] | None = None,
    extra_members: list[tuple[str, str]] | None = None,
) -> sqlite3.Row:
    clean_slug = slug.strip().lower()
    if not is_valid_slug(clean_slug):
        raise ValidationError(f"Invalid board slug '{slug}'; use lowercase alphanumeric and hyphens")
    if not name.strip():
        raise ValidationError("Board name is required")
    if not purpose.strip():
        raise ValidationError("Board purpose is required")

    if boards_store.get_board_by_slug(conn, clean_slug) is not None:
        raise ConflictError(f"Board '{clean_slug}' already exists", code="duplicate_board")

    merged_settings = dict(_DEFAULT_SETTINGS)
    if settings:
        merged_settings.update(settings)

    now_iso = utc_now_iso()
    board_id = new_ulid()
    boards_store.insert_board(
        conn,
        board_id=board_id,
        slug=clean_slug,
        name=name.strip(),
        purpose=purpose.strip(),
        settings_json=json.dumps(merged_settings),
        created_at=now_iso,
    )

    # Grant owner membership to creator and any server owners
    if ctx.auth_kind != "system":
        boards_store.upsert_membership(
            conn,
            board_id=board_id,
            principal_id=ctx.principal_id,
            role="owner",
            created_at=now_iso,
        )
    for p in principals_store.list_principals(conn):
        if p["is_server_owner"] and p["disabled_at"] is None:
            boards_store.upsert_membership(
                conn,
                board_id=board_id,
                principal_id=p["id"],
                role="owner",
                created_at=now_iso,
            )

    if extra_members:
        for handle, role in extra_members:
            p = principals_store.get_principal_by_handle(conn, handle)
            if p is not None and p["kind"] != "system":
                boards_store.upsert_membership(
                    conn,
                    board_id=board_id,
                    principal_id=p["id"],
                    role=role,
                    created_at=now_iso,
                )

    creator_id = ctx.principal_id
    for default_title in ("general", "decisions"):
        boards_store.insert_thread(
            conn,
            thread_id=new_ulid(),
            board_id=board_id,
            title=default_title,
            created_by=creator_id,
            created_at=now_iso,
        )

    record_audit(
        conn,
        ctx=ctx,
        action="board.create",
        ok=True,
        board_id=board_id,
        target=clean_slug,
    )
    row = boards_store.get_board_stats_for_principal(conn, board_id, ctx.principal_id)
    assert row is not None
    return row


def create_board(
    services: AppServices,
    ctx: AuthContext,
    *,
    slug: str,
    name: str,
    purpose: str,
    settings: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    require_server_owner(ctx)
    with services.db.write_tx() as conn:

        def _do() -> dict[str, Any]:
            row = create_board_in_tx(
                conn,
                ctx=ctx,
                slug=slug,
                name=name,
                purpose=purpose,
                settings=settings,
            )
            return board_row_to_dict(row, ctx)

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation="board.create",
            request_payload={"slug": slug, "name": name, "purpose": purpose, "settings": settings},
            status_code=201,
            fn=_do,
        )
        return res


def list_boards(services: AppServices, ctx: AuthContext, *, include_archived: bool = False) -> list[dict[str, Any]]:
    rows = boards_store.list_boards_for_principal(
        services.db.read_conn(),
        ctx.principal_id,
        include_archived=include_archived,
        is_server_owner=ctx.is_server_owner,
    )
    visible: list[dict[str, Any]] = []
    for row in rows:
        if not ctx.allows_board(row["slug"]):
            continue
        if ctx.effective_role(row["membership_role"]) is None:
            continue
        visible.append(board_row_to_dict(row, ctx))
    return visible


def get_board(services: AppServices, ctx: AuthContext, *, board_slug: str) -> dict[str, Any]:
    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    row = boards_store.get_board_stats_for_principal(conn, board["id"], ctx.principal_id)
    assert row is not None
    return board_row_to_dict(row, ctx)


def update_board(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    name: str | None = None,
    purpose: str | None = None,
    settings: dict[str, Any] | None = None,
    archived: bool | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "owner", allow_archived=True)

        def _do() -> dict[str, Any]:
            new_name = name.strip() if name is not None else board["name"]
            new_purpose = purpose.strip() if purpose is not None else board["purpose"]
            if not new_name or not new_purpose:
                raise ValidationError("Board name and purpose cannot be empty")
            cur_settings = _parse_settings(board["settings_json"])
            if settings is not None:
                cur_settings.update(settings)
            new_archived = board["archived_at"]
            if archived is True and new_archived is None:
                new_archived = utc_now_iso()
            elif archived is False:
                new_archived = None

            boards_store.update_board(
                conn,
                board_id=board["id"],
                name=new_name,
                purpose=new_purpose,
                settings_json=json.dumps(cur_settings),
                archived_at=new_archived,
            )
            record_audit(
                conn,
                ctx=ctx,
                action="board.update",
                ok=True,
                board_id=board["id"],
                target=board["slug"],
            )
            row = boards_store.get_board_stats_for_principal(conn, board["id"], ctx.principal_id)
            assert row is not None
            return board_row_to_dict(row, ctx)

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"board.update:{board['slug']}",
            request_payload={"name": name, "purpose": purpose, "settings": settings, "archived": archived},
            status_code=200,
            fn=_do,
        )
        return res


def list_threads(
    services: AppServices, ctx: AuthContext, *, board_slug: str, state: str = "open"
) -> list[dict[str, Any]]:
    if state not in ("open", "closed", "all"):
        raise ValidationError("state must be 'open', 'closed', or 'all'")
    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    rows = boards_store.list_threads(conn, board["id"], state=state)
    return [thread_row_to_dict(r, board["slug"]) for r in rows]


def create_thread(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    title: str,
    first_message: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    clean_title = title.strip()
    if not clean_title:
        raise ValidationError("Thread title is required")
    if len(clean_title) > 200:
        raise ValidationError("Thread title must be <= 200 characters")

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent")

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            thread_id = new_ulid()
            boards_store.insert_thread(
                conn,
                thread_id=thread_id,
                board_id=board["id"],
                title=clean_title,
                created_by=ctx.principal_id,
                created_at=now_iso,
            )
            created_msg: dict[str, Any] | None = None
            if first_message is not None:
                from agent_board.services.messages import post_message_in_tx

                created_msg = post_message_in_tx(
                    conn,
                    services=services,
                    ctx=ctx,
                    board=board,
                    thread_id=thread_id,
                    msg_type=str(first_message.get("type", "note")),
                    body_md=str(first_message.get("body_md", "")),
                    data=first_message.get("data"),
                    reply_to_id=first_message.get("reply_to_id"),
                    attachment_ids=first_message.get("attachment_ids"),
                )
            record_audit(
                conn,
                ctx=ctx,
                action="thread.create",
                ok=True,
                board_id=board["id"],
                target=thread_id,
                detail={"title": clean_title},
            )
            row = boards_store.get_thread_by_id(conn, thread_id)
            assert row is not None
            out = thread_row_to_dict(row, board["slug"])
            if created_msg is not None:
                out["first_message"] = created_msg
            return out

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"thread.create:{board['slug']}",
            request_payload={"title": clean_title, "first_message": first_message},
            status_code=201,
            fn=_do,
        )
        return res


def close_thread(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    thread_id: str,
    closed: bool = True,
) -> dict[str, Any]:
    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent")
        thread = boards_store.get_thread_by_id(conn, thread_id)
        if thread is None or thread["board_id"] != board["id"]:
            raise NotFoundError(f"Thread '{thread_id}' not found on board '{board_slug}'")
        closed_at = utc_now_iso() if closed else None
        boards_store.set_thread_closed(conn, thread_id, closed_at)
        record_audit(
            conn,
            ctx=ctx,
            action="thread.close" if closed else "thread.reopen",
            ok=True,
            board_id=board["id"],
            target=thread_id,
        )
        updated = boards_store.get_thread_by_id(conn, thread_id)
        assert updated is not None
        return thread_row_to_dict(updated, board["slug"])
