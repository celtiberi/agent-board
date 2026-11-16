"""Domain service for append-only messages, edits, mentions, and redaction."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import jsonschema

from agent_board.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from agent_board.ids import new_ulid, utc_now_iso
from agent_board.secrets_scan import scan_for_secrets
from agent_board.services.auth import record_audit, require_board_role
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.delivery import enqueue_outbox_for_message
from agent_board.services.idempotency import execute_idempotent
from agent_board.store import boards as boards_store
from agent_board.store import delivery as delivery_store
from agent_board.store import messages as messages_store
from agent_board.store import principals as principals_store

VALID_MESSAGE_TYPES = frozenset(
    {
        "note",
        "task",
        "question",
        "decision",
        "issue",
        "status",
        "retest_report",
        "summary",
        "system",
    }
)
DIRECT_POST_TYPES = frozenset({"note", "task", "question", "status", "retest_report", "summary"})
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_.-])@([a-z0-9][a-z0-9_-]{0,48}[a-z0-9]?)")


def validate_message_data(services: AppServices, msg_type: str, data: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValidationError("message data must be a JSON object")
    schema = services.message_schemas.get(msg_type)
    if schema:
        try:
            jsonschema.validate(instance=data, schema=schema)
        except jsonschema.ValidationError as exc:
            raise ValidationError(f"Invalid data for type '{msg_type}': {exc.message}") from exc
    return data


def _extract_mentions(conn: sqlite3.Connection, body_md: str) -> list[sqlite3.Row]:
    handles = list(dict.fromkeys(_MENTION_RE.findall(body_md.lower())))
    if not handles:
        return []
    rows = principals_store.get_principals_by_handles(conn, handles)
    return [r for r in rows if r["kind"] != "system"]


def _hydrate_messages(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    if not rows:
        return []
    msg_ids = [r["id"] for r in rows]
    mentions_map = messages_store.get_mentions_for_messages(conn, msg_ids)
    attachments_map = messages_store.get_attachments_for_messages(conn, msg_ids)
    result: list[dict[str, Any]] = []
    for r in rows:
        keys = set(r.keys())
        version_count = int(r["version_count"]) if "version_count" in keys else 1
        edited_at = r["latest_created_at"] if ("latest_created_at" in keys and version_count > 1) else None
        atts = [
            {
                "id": a["id"],
                "filename": a["filename"],
                "content_type": a["content_type"],
                "size_bytes": int(a["size_bytes"]),
                "sha256": a["sha256"],
            }
            for a in attachments_map.get(r["id"], [])
        ]
        result.append(
            {
                "id": r["id"],
                "board_id": r["board_id"],
                "board_slug": r["board_slug"],
                "thread_id": r["thread_id"],
                "thread_title": r["thread_title"],
                "seq": int(r["seq"]),
                "author_id": r["author_id"],
                "author_handle": r["author_handle"],
                "type": r["type"],
                "body_md": r["body_md"],
                "data": json.loads(r["data"]) if r["data"] is not None else None,
                "warnings": json.loads(r["warnings_json"]),
                "mentions": mentions_map.get(r["id"], []),
                "attachments": atts,
                "reply_to_id": r["reply_to_id"],
                "supersedes_id": r["supersedes_id"],
                "root_id": r["root_id"],
                "root_seq": int(r["root_seq"]),
                "is_edit": bool(r["supersedes_id"]),
                "version_count": version_count,
                "created_at": r["created_at"],
                "edited_at": edited_at,
                "redacted_at": r["redacted_at"],
            }
        )
    return result


def resolve_or_create_thread(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    created_by: str,
    thread_id: str | None = None,
    thread_title: str | None = None,
    now_iso: str,
) -> sqlite3.Row:
    if thread_id:
        thread = boards_store.get_thread_by_id(conn, thread_id)
        if thread is None or thread["board_id"] != board_id:
            raise NotFoundError(f"Thread '{thread_id}' not found on this board")
        if thread["closed_at"] is not None:
            raise ValidationError("Thread is closed and read-only", code="thread_closed")
        return thread

    title = (thread_title or "general").strip()
    if not title:
        title = "general"
    existing = boards_store.get_thread_by_title(conn, board_id, title)
    if existing is not None:
        if existing["closed_at"] is not None:
            raise ValidationError(f"Thread '{title}' is closed and read-only", code="thread_closed")
        return existing

    new_tid = new_ulid()
    boards_store.insert_thread(
        conn,
        thread_id=new_tid,
        board_id=board_id,
        title=title,
        created_by=created_by,
        created_at=now_iso,
    )
    created = boards_store.get_thread_by_id(conn, new_tid)
    assert created is not None
    return created


def post_message_in_tx(
    conn: sqlite3.Connection,
    *,
    services: AppServices,
    ctx: AuthContext,
    board: sqlite3.Row,
    msg_type: str,
    body_md: str,
    thread_id: str | None = None,
    thread_title: str | None = None,
    data: dict[str, Any] | None = None,
    reply_to_id: str | None = None,
    attachment_ids: list[str] | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    if msg_type not in VALID_MESSAGE_TYPES:
        raise ValidationError(f"Invalid message type '{msg_type}'")
    if not body_md or not body_md.strip():
        raise ValidationError("body_md must not be empty")
    if len(body_md.encode("utf-8")) > services.settings.max_message_bytes:
        raise ValidationError(f"Message body exceeds limit of {services.settings.max_message_bytes} bytes")

    validated_data = validate_message_data(services, msg_type, data)
    ts = now_iso or utc_now_iso()
    thread = resolve_or_create_thread(
        conn,
        board_id=board["id"],
        created_by=ctx.principal_id,
        thread_id=thread_id,
        thread_title=thread_title,
        now_iso=ts,
    )

    if reply_to_id:
        parent = messages_store.get_message_by_id(conn, reply_to_id)
        if parent is None or parent["board_id"] != board["id"]:
            raise NotFoundError(f"reply_to_id '{reply_to_id}' not found on this board")

    clean_att_ids = list(dict.fromkeys(attachment_ids or []))
    if clean_att_ids:
        for att_id in clean_att_ids:
            att = messages_store.get_attachment_by_id(conn, att_id)
            if att is None or att["board_id"] != board["id"]:
                raise NotFoundError(f"Attachment '{att_id}' not found on this board")
            if att["message_id"] is not None:
                raise ConflictError(f"Attachment '{att_id}' is already attached to a message")

    warnings = scan_for_secrets(body_md)
    mentioned_rows = _extract_mentions(conn, body_md)
    mentioned_ids = [r["id"] for r in mentioned_rows]

    msg_id = new_ulid()
    seq = boards_store.allocate_next_seq(conn, board["id"])
    root_rowid = messages_store.insert_message(
        conn,
        message_id=msg_id,
        board_id=board["id"],
        thread_id=thread["id"],
        seq=seq,
        author_id=ctx.principal_id,
        msg_type=msg_type,
        body_md=body_md,
        data_json=json.dumps(validated_data) if validated_data is not None else None,
        warnings_json=json.dumps(warnings),
        reply_to_id=reply_to_id,
        supersedes_id=None,
        root_id=msg_id,
        root_seq=seq,
        created_at=ts,
    )

    if clean_att_ids:
        linked = messages_store.link_attachments_to_message(
            conn, attachment_ids=clean_att_ids, board_id=board["id"], message_id=msg_id
        )
        if linked != len(clean_att_ids):
            raise ConflictError("One or more attachments could not be linked")

    messages_store.insert_mentions(conn, msg_id, mentioned_ids)
    messages_store.upsert_message_fts(
        conn,
        root_rowid=root_rowid,
        message_id=msg_id,
        root_id=msg_id,
        board_id=board["id"],
        thread_id=thread["id"],
        thread_title=thread["title"],
        body_md=body_md,
    )
    enqueue_outbox_for_message(
        conn,
        board_id=board["id"],
        board_slug=board["slug"],
        seq=seq,
        author_id=ctx.principal_id,
        msg_type=msg_type,
        mentioned_principal_ids=set(mentioned_ids),
        now_iso=ts,
    )
    row = messages_store.get_message_by_id(conn, msg_id)
    assert row is not None
    return _hydrate_messages(conn, [row])[0]


def post_message(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    msg_type: str,
    body_md: str,
    thread_id: str | None = None,
    thread_title: str | None = None,
    data: dict[str, Any] | None = None,
    reply_to_id: str | None = None,
    attachment_ids: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if msg_type not in DIRECT_POST_TYPES:
        raise ValidationError(f"Type '{msg_type}' cannot be posted directly; use record_decision or issue tools")

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent")

        def _do() -> dict[str, Any]:
            msg = post_message_in_tx(
                conn,
                services=services,
                ctx=ctx,
                board=board,
                msg_type=msg_type,
                body_md=body_md,
                thread_id=thread_id,
                thread_title=thread_title,
                data=data,
                reply_to_id=reply_to_id,
                attachment_ids=attachment_ids,
            )
            record_audit(
                conn,
                ctx=ctx,
                action="message.post",
                ok=True,
                board_id=board["id"],
                target=msg["id"],
                detail={"seq": msg["seq"], "type": msg_type},
            )
            return msg

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"message.post:{board['slug']}",
            request_payload={
                "thread_id": thread_id,
                "thread_title": thread_title,
                "type": msg_type,
                "body_md": body_md,
                "data": data,
                "reply_to_id": reply_to_id,
                "attachment_ids": attachment_ids,
            },
            status_code=201,
            fn=_do,
        )
        return res


def edit_message(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    message_id: str,
    body_md: str,
    data: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not body_md or not body_md.strip():
        raise ValidationError("body_md must not be empty")
    if len(body_md.encode("utf-8")) > services.settings.max_message_bytes:
        raise ValidationError(f"Message body exceeds limit of {services.settings.max_message_bytes} bytes")

    with services.db.write_tx() as conn:
        board, eff_role = require_board_role(conn, ctx, board_slug, "agent")
        target = messages_store.get_message_by_id(conn, message_id)
        if target is None or target["board_id"] != board["id"]:
            raise NotFoundError(f"Message '{message_id}' not found on board '{board_slug}'")
        if target["author_id"] != ctx.principal_id and eff_role != "owner":
            raise PermissionDeniedError("Only the message author or board owner can edit this message")
        if target["redacted_at"] is not None:
            raise ValidationError("Cannot edit a redacted message")
        if target["type"] in ("decision", "issue", "system"):
            raise ValidationError(
                f"Cannot edit '{target['type']}' messages directly; use supersede_decision or issue tools"
            )

        thread = boards_store.get_thread_by_id(conn, target["thread_id"])
        if thread is None or thread["closed_at"] is not None:
            raise ValidationError("Thread is closed and read-only", code="thread_closed")

        latest = messages_store.get_latest_message_for_root(conn, target["root_id"])
        assert latest is not None
        if latest["id"] != target["id"]:
            raise ConflictError(
                f"Message '{message_id}' was already edited; edit latest version '{latest['id']}'",
                code="already_superseded",
            )

        def _do() -> dict[str, Any]:
            validated_data = (
                validate_message_data(services, target["type"], data)
                if data is not None
                else (json.loads(target["data"]) if target["data"] is not None else None)
            )
            now_iso = utc_now_iso()
            warnings = scan_for_secrets(body_md)
            mentioned_rows = _extract_mentions(conn, body_md)
            mentioned_ids = [r["id"] for r in mentioned_rows]

            new_id = new_ulid()
            seq = boards_store.allocate_next_seq(conn, board["id"])
            root_rowid = messages_store.insert_message(
                conn,
                message_id=new_id,
                board_id=board["id"],
                thread_id=target["thread_id"],
                seq=seq,
                author_id=ctx.principal_id,
                msg_type=target["type"],
                body_md=body_md,
                data_json=json.dumps(validated_data) if validated_data is not None else None,
                warnings_json=json.dumps(warnings),
                reply_to_id=target["reply_to_id"],
                supersedes_id=target["id"],
                root_id=target["root_id"],
                root_seq=int(target["root_seq"]),
                created_at=now_iso,
            )
            messages_store.insert_mentions(conn, new_id, mentioned_ids)
            messages_store.upsert_message_fts(
                conn,
                root_rowid=root_rowid,
                message_id=new_id,
                root_id=target["root_id"],
                board_id=board["id"],
                thread_id=target["thread_id"],
                thread_title=thread["title"],
                body_md=body_md,
            )
            enqueue_outbox_for_message(
                conn,
                board_id=board["id"],
                board_slug=board["slug"],
                seq=seq,
                author_id=ctx.principal_id,
                msg_type=target["type"],
                mentioned_principal_ids=set(mentioned_ids),
                now_iso=now_iso,
            )
            record_audit(
                conn,
                ctx=ctx,
                action="message.edit",
                ok=True,
                board_id=board["id"],
                target=new_id,
                detail={"supersedes_id": target["id"], "root_id": target["root_id"]},
            )
            row = messages_store.get_message_by_id(conn, new_id)
            assert row is not None
            return _hydrate_messages(conn, [row])[0]

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"message.edit:{board['slug']}:{message_id}",
            request_payload={"body_md": body_md, "data": data},
            status_code=200,
            fn=_do,
        )
        return res


def redact_message(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    message_id: str,
    reason: str,
) -> dict[str, Any]:
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValidationError("Redaction reason is required")

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "owner", allow_archived=True)
        target = messages_store.get_message_by_id(conn, message_id)
        if target is None or target["board_id"] != board["id"]:
            raise NotFoundError(f"Message '{message_id}' not found on board '{board_slug}'")

        now_iso = utc_now_iso()
        date_str = now_iso[:10]
        redacted_body = f"[redacted by {ctx.handle} on {date_str}: {clean_reason}]"
        redacted_rows = messages_store.redact_message_chain(
            conn,
            root_id=target["root_id"],
            redacted_body=redacted_body,
            redacted_at=now_iso,
        )
        messages_store.delete_message_fts_by_root(conn, target["root_id"])
        record_audit(
            conn,
            ctx=ctx,
            action="message.redact",
            ok=True,
            board_id=board["id"],
            target=target["root_id"],
            detail={"reason": clean_reason, "versions_redacted": len(redacted_rows)},
        )
        latest = messages_store.get_latest_message_for_root(conn, target["root_id"])
        assert latest is not None
        return _hydrate_messages(conn, [latest])[0]


def read_messages(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    since_seq: int | None = None,
    thread_id: str | None = None,
    types: list[str] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        raise ValidationError("limit must be between 1 and 200")
    if types:
        bad = [t for t in types if t not in VALID_MESSAGE_TYPES]
        if bad:
            raise ValidationError(f"Invalid message types: {', '.join(bad)}")

    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    effective_since = (
        since_seq if since_seq is not None else delivery_store.get_cursor(conn, ctx.principal_id, board["id"])
    )
    if effective_since < 0:
        raise ValidationError("since_seq must be >= 0")

    rows = messages_store.read_messages_stream(
        conn,
        board_id=board["id"],
        since_seq=effective_since,
        thread_id=thread_id,
        types=types,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    messages = _hydrate_messages(conn, page_rows)
    next_since_seq = messages[-1]["seq"] if messages else effective_since
    return {
        "board": board["slug"],
        "since_seq": effective_since,
        "next_since_seq": next_since_seq,
        "has_more": has_more,
        "messages": messages,
    }


def get_thread_snapshot(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    thread_id: str,
    since_seq: int | None = None,
) -> dict[str, Any]:
    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    thread = boards_store.get_thread_by_id(conn, thread_id)
    if thread is None or thread["board_id"] != board["id"]:
        raise NotFoundError(f"Thread '{thread_id}' not found on board '{board_slug}'")

    if since_seq is not None:
        rows = messages_store.read_messages_stream(
            conn,
            board_id=board["id"],
            since_seq=since_seq,
            thread_id=thread_id,
            limit=200,
        )
    else:
        rows = messages_store.read_thread_snapshot(conn, thread_id)

    messages = _hydrate_messages(conn, rows)
    from agent_board.services.boards import thread_row_to_dict

    return {
        "thread": thread_row_to_dict(thread, board["slug"]),
        "messages": messages,
    }


def get_message_with_history(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    message_id: str,
) -> dict[str, Any]:
    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    target = messages_store.get_message_by_id(conn, message_id)
    if target is None or target["board_id"] != board["id"]:
        raise NotFoundError(f"Message '{message_id}' not found on board '{board_slug}'")
    versions = messages_store.get_message_versions_by_root(conn, target["root_id"])
    hydrated = _hydrate_messages(conn, versions)
    requested = next(m for m in hydrated if m["id"] == message_id)
    return {
        "message": requested,
        "versions": hydrated,
    }
