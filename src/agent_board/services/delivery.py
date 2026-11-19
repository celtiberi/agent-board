"""Cursors, subscriptions, and transactional outbox queuing."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from agent_board.errors import NotFoundError, PermissionDeniedError, ValidationError
from agent_board.ids import dt_to_iso, iso_to_dt, new_ulid, utc_now_iso
from agent_board.services.auth import record_audit, require_board_role
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.idempotency import execute_idempotent
from agent_board.store import delivery as delivery_store


def ack_cursor(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    read_seq: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if read_seq is not None and read_seq < 0:
        raise ValidationError("read_seq must be >= 0")

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
        latest_seq = int(board["next_seq"]) - 1
        target_seq = latest_seq if read_seq is None else read_seq
        if target_seq > latest_seq:
            raise ValidationError(
                f"Cannot ack read_seq={target_seq} past latest board seq={latest_seq}",
                code="invalid_read_seq",
            )

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            effective_seq = delivery_store.upsert_cursor(
                conn,
                principal_id=ctx.principal_id,
                board_id=board["id"],
                read_seq=target_seq,
                updated_at=now_iso,
            )
            delivery_store.heal_principal_delivery_on_activity(
                conn,
                principal_id=ctx.principal_id,
                now_iso=now_iso,
                board_id=board["id"],
                acked_seq=effective_seq,
            )
            unread = delivery_store.count_unread_for_principal(conn, board["id"], ctx.principal_id, effective_seq)
            record_audit(
                conn,
                ctx=ctx,
                action="cursor.ack",
                ok=True,
                board_id=board["id"],
                target=str(effective_seq),
            )
            return {
                "board": board["slug"],
                "read_seq": effective_seq,
                "latest_seq": latest_seq,
                "unread_count": unread,
                "updated_at": now_iso,
            }

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"ack:{board['slug']}",
            request_payload={"read_seq": target_seq},
            status_code=200,
            fn=_do,
        )
        return res


def subscription_matches_message(
    sub_row: sqlite3.Row,
    *,
    board_slug: str,
    author_id: str,
    msg_type: str,
    mentioned_principal_ids: set[str],
    is_broadcast: bool = False,
    is_directed: bool = False,
) -> bool:
    sub_boards: list[str] = json.loads(sub_row["boards"])
    if "*" not in sub_boards and board_slug not in sub_boards:
        return False

    flt: dict[str, Any] = json.loads(sub_row["filter"])
    exclude_self = bool(flt.get("exclude_self", True))
    if exclude_self and sub_row["principal_id"] == author_id:
        return False

    if is_broadcast:
        return True

    is_mentioned = sub_row["principal_id"] in mentioned_principal_ids
    if mentioned_principal_ids or is_directed:
        return is_mentioned

    mentions_only = bool(flt.get("mentions_only", False))
    if mentions_only:
        return False

    allowed_types = flt.get("types")
    if allowed_types is not None and len(allowed_types) > 0:
        if msg_type not in allowed_types:
            return False

    return True


def enqueue_outbox_for_message(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    board_slug: str,
    seq: int,
    author_id: str,
    msg_type: str,
    mentioned_principal_ids: set[str],
    is_broadcast: bool = False,
    is_directed: bool = False,
    now_iso: str,
) -> None:
    """Enqueue or coalesce outbox nudges inside the caller's write transaction."""
    now_dt = iso_to_dt(now_iso)
    subs = delivery_store.list_active_subscriptions_for_board(conn, board_id)
    for sub in subs:
        if not subscription_matches_message(
            sub,
            board_slug=board_slug,
            author_id=author_id,
            msg_type=msg_type,
            mentioned_principal_ids=mentioned_principal_ids,
            is_broadcast=is_broadcast,
            is_directed=is_directed,
        ):
            continue
        debounce_dt = now_dt + timedelta(seconds=int(sub["debounce_s"]))
        next_attempt = debounce_dt
        if sub["last_sent_at"]:
            interval_dt = iso_to_dt(sub["last_sent_at"]) + timedelta(seconds=int(sub["min_interval_s"]))
            next_attempt = max(next_attempt, interval_dt)
        if int(sub["failure_count"]) > 0 and sub["updated_at"]:
            reprobe_floor_s = max(int(sub["min_interval_s"]), 300)
            reprobe_dt = iso_to_dt(sub["updated_at"]) + timedelta(seconds=reprobe_floor_s)
            next_attempt = max(next_attempt, reprobe_dt)

        delivery_store.enqueue_outbox_pending(
            conn,
            outbox_id=new_ulid(),
            subscription_id=sub["id"],
            board_id=board_id,
            latest_seq=seq,
            next_attempt_at=dt_to_iso(next_attempt),
            created_at=now_iso,
        )


def normalize_auth_header(raw: str | None) -> str | None:
    """Strip surrounding whitespace and any accidental leading 'Authorization:' header name."""
    if raw is None:
        return None
    val = raw.strip()
    if val.lower().startswith("authorization:"):
        val = val.split(":", 1)[1].strip()
    return val or None


def _validate_webhook_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("Subscription url must be a valid http(s) URL")
    return url.strip()


def create_subscription(
    services: AppServices,
    ctx: AuthContext,
    *,
    url: str,
    boards: list[str] | None = None,
    filter_cfg: dict[str, Any] | None = None,
    auth_header: str | None = None,
    hmac_secret: str | None = None,
    min_interval_s: int = 60,
    debounce_s: int = 10,
    payload_format: str = "default",
    principal_handle: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    clean_url = _validate_webhook_url(url)
    if payload_format != "default":
        raise ValidationError("payload_format must be 'default'")
    if min_interval_s < 0 or debounce_s < 0:
        raise ValidationError("min_interval_s and debounce_s must be >= 0")

    target_principal_id = ctx.principal_id
    if principal_handle and principal_handle != ctx.handle:
        if not ctx.is_server_owner:
            raise PermissionDeniedError("Only the server owner can manage other principals' subscriptions")
        from agent_board.store import principals as principals_store

        p = principals_store.get_principal_by_handle(services.db.read_conn(), principal_handle)
        if p is None or p["kind"] == "system":
            raise NotFoundError(f"Principal '{principal_handle}' not found")
        target_principal_id = p["id"]
    elif not ctx.is_server_owner:
        # Must be at least agent on at least one board (checked against token max_role)
        if ctx.max_role == "readonly":
            raise PermissionDeniedError("Readonly tokens cannot manage subscriptions")

    clean_boards = [b.strip() for b in (boards or ["*"]) if b.strip()] or ["*"]
    clean_filter = dict(filter_cfg or {})
    clean_filter.setdefault("exclude_self", True)
    clean_filter.setdefault("mentions_only", False)

    clean_auth_header = normalize_auth_header(auth_header)
    secret_plain = hmac_secret or f"whsec_{secrets.token_urlsafe(32)}"

    with services.db.write_tx() as conn:

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            sub_id = new_ulid()
            auth_enc = services.crypto.encrypt(clean_auth_header) if clean_auth_header else None
            hmac_enc = services.crypto.encrypt(secret_plain)
            delivery_store.insert_subscription(
                conn,
                sub_id=sub_id,
                principal_id=target_principal_id,
                url=clean_url,
                auth_header_enc=auth_enc,
                hmac_secret_enc=hmac_enc,
                payload_format=payload_format,
                boards_json=json.dumps(clean_boards),
                filter_json=json.dumps(clean_filter),
                min_interval_s=min_interval_s,
                debounce_s=debounce_s,
                now_iso=now_iso,
            )
            record_audit(
                conn,
                ctx=ctx,
                action="subscription.create",
                ok=True,
                target=sub_id,
                detail={"url": clean_url, "boards": clean_boards},
            )
            row = delivery_store.get_subscription_by_id(conn, sub_id)
            out = subscription_to_dict(row)
            out["hmac_secret"] = secret_plain
            return out

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation="subscription.create",
            request_payload={
                "principal": target_principal_id,
                "url": clean_url,
                "boards": clean_boards,
                "filter": clean_filter,
                "min_interval_s": min_interval_s,
                "debounce_s": debounce_s,
            },
            status_code=201,
            fn=_do,
        )
        return res


def list_subscriptions(
    services: AppServices, ctx: AuthContext, *, all_principals: bool = False
) -> list[dict[str, Any]]:
    conn = services.db.read_conn()
    if all_principals and ctx.is_server_owner:
        rows = delivery_store.list_all_subscriptions(conn)
    else:
        rows = delivery_store.list_subscriptions_for_principal(conn, ctx.principal_id)
    return [subscription_to_dict(r) for r in rows]


def delete_subscription(services: AppServices, ctx: AuthContext, *, sub_id: str) -> dict[str, Any]:
    with services.db.write_tx() as conn:
        row = delivery_store.get_subscription_by_id(conn, sub_id)
        if row is None:
            raise NotFoundError(f"Subscription '{sub_id}' not found")
        if row["principal_id"] != ctx.principal_id and not ctx.is_server_owner:
            raise PermissionDeniedError("Cannot delete another principal's subscription")
        delivery_store.delete_subscription(conn, sub_id)
        record_audit(conn, ctx=ctx, action="subscription.delete", ok=True, target=sub_id)
    return {"id": sub_id, "deleted": True}


def update_subscription(
    services: AppServices,
    ctx: AuthContext,
    *,
    sub_id: str,
    url: str | None = None,
    boards: list[str] | None = None,
    filter_cfg: dict[str, Any] | None = None,
    auth_header: str | None = None,
    clear_auth_header: bool = False,
    min_interval_s: int | None = None,
    debounce_s: int | None = None,
) -> dict[str, Any]:
    """Update mutable webhook subscription fields in place while preserving ID and delivery cursor."""
    if min_interval_s is not None and min_interval_s < 0:
        raise ValidationError("min_interval_s must be >= 0")
    if debounce_s is not None and debounce_s < 0:
        raise ValidationError("debounce_s must be >= 0")
    clean_url_input = _validate_webhook_url(url) if url is not None else None

    with services.db.write_tx() as conn:
        row = delivery_store.get_subscription_by_id(conn, sub_id)
        if row is None:
            raise NotFoundError(f"Subscription '{sub_id}' not found")
        if row["principal_id"] != ctx.principal_id and not ctx.is_server_owner:
            raise PermissionDeniedError("Cannot update another principal's subscription")

        next_url = clean_url_input if clean_url_input is not None else str(row["url"])
        next_boards = (
            [b.strip() for b in boards if b.strip()] or ["*"]
            if boards is not None
            else [str(b) for b in json.loads(row["boards"])]
        )
        next_filter = dict(json.loads(row["filter"]))
        if filter_cfg is not None:
            for k, v in filter_cfg.items():
                if v is None:
                    next_filter.pop(k, None)
                else:
                    next_filter[k] = v
        next_filter.setdefault("exclude_self", True)
        next_filter.setdefault("mentions_only", False)

        next_interval = int(min_interval_s) if min_interval_s is not None else int(row["min_interval_s"])
        next_debounce = int(debounce_s) if debounce_s is not None else int(row["debounce_s"])

        update_auth = clear_auth_header or auth_header is not None
        auth_enc: str | None = None
        if update_auth and not clear_auth_header:
            clean_auth = normalize_auth_header(auth_header)
            if clean_auth:
                auth_enc = services.crypto.encrypt(clean_auth)

        now_iso = utc_now_iso()
        delivery_store.update_subscription(
            conn,
            sub_id=sub_id,
            url=next_url,
            auth_header_enc=auth_enc,
            update_auth_header=update_auth,
            boards_json=json.dumps(next_boards),
            filter_json=json.dumps(next_filter),
            min_interval_s=next_interval,
            debounce_s=next_debounce,
            now_iso=now_iso,
        )
        record_audit(
            conn,
            ctx=ctx,
            action="subscription.update",
            ok=True,
            target=sub_id,
            detail={"url": next_url, "boards": next_boards},
        )
        updated = delivery_store.get_subscription_by_id(conn, sub_id)
        return subscription_to_dict(updated)


def reenable_subscription(services: AppServices, ctx: AuthContext, *, sub_id: str) -> dict[str, Any]:
    with services.db.write_tx() as conn:
        row = delivery_store.get_subscription_by_id(conn, sub_id)
        if row is None:
            raise NotFoundError(f"Subscription '{sub_id}' not found")
        if row["principal_id"] != ctx.principal_id and not ctx.is_server_owner:
            raise PermissionDeniedError("Cannot modify another principal's subscription")
        delivery_store.heal_subscription_delivery(
            conn,
            sub_id=sub_id,
            now_iso=utc_now_iso(),
            force_enable=True,
        )
        record_audit(conn, ctx=ctx, action="subscription.enable", ok=True, target=sub_id)
        return subscription_to_dict(delivery_store.get_subscription_by_id(conn, sub_id))


def subscription_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "principal_id": row["principal_id"],
        "principal_handle": row["principal_handle"],
        "url": row["url"],
        "has_auth_header": bool(row["auth_header_enc"]),
        "payload_format": row["payload_format"],
        "boards": json.loads(row["boards"]),
        "filter": json.loads(row["filter"]),
        "min_interval_s": int(row["min_interval_s"]),
        "debounce_s": int(row["debounce_s"]),
        "active": bool(row["active"]),
        "failure_count": int(row["failure_count"]),
        "disabled_reason": row["disabled_reason"],
        "last_sent_at": row["last_sent_at"],
        "created_at": row["created_at"],
    }
