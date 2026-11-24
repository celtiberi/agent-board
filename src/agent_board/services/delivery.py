"""Cursors, subscriptions, and transactional outbox queuing."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from agent_board.errors import NotFoundError, PermissionDeniedError, ValidationError
from agent_board.ids import dt_to_iso, iso_to_dt, new_ulid, utc_now, utc_now_iso
from agent_board.services.auth import record_audit, require_board_role
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.idempotency import execute_idempotent
from agent_board.store import delivery as delivery_store
from agent_board.store import principals as principals_store

MAX_DND_MINUTES = 240
DEFAULT_DND_MINUTES = 30
MAX_SUB_INTERVAL_S = 86_400
_VALID_FILTER_KEYS = frozenset({"exclude_self", "mentions_only", "types"})
_VALID_FILTER_MSG_TYPES = frozenset(
    {"note", "task", "question", "decision", "issue", "status", "retest_report", "summary", "system"}
)


def _validate_filter_cfg(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"exclude_self": True, "mentions_only": False}
    if not isinstance(raw, dict):
        raise ValidationError("Subscription filter must be a JSON object")
    unknown = set(raw.keys()) - _VALID_FILTER_KEYS
    if unknown:
        raise ValidationError(f"Unknown subscription filter key(s): {', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    if "exclude_self" in raw and raw["exclude_self"] is not None:
        if not isinstance(raw["exclude_self"], bool):
            raise ValidationError("filter.exclude_self must be a boolean")
        cleaned["exclude_self"] = raw["exclude_self"]
    if "mentions_only" in raw and raw["mentions_only"] is not None:
        if not isinstance(raw["mentions_only"], bool):
            raise ValidationError("filter.mentions_only must be a boolean")
        cleaned["mentions_only"] = raw["mentions_only"]
    if "types" in raw and raw["types"] is not None:
        if not isinstance(raw["types"], list) or not all(isinstance(t, str) for t in raw["types"]):
            raise ValidationError("filter.types must be a list of message type strings")
        bad = [t for t in raw["types"] if t not in _VALID_FILTER_MSG_TYPES]
        if bad:
            raise ValidationError(f"Invalid message type(s) in filter.types: {', '.join(bad)}")
        cleaned["types"] = list(dict.fromkeys(raw["types"]))
    cleaned.setdefault("exclude_self", True)
    cleaned.setdefault("mentions_only", False)
    return cleaned


def _validate_boards_for_caller(conn: sqlite3.Connection, ctx: AuthContext, boards: list[str]) -> None:
    if ctx.has_server_owner_privilege:
        return
    for slug in boards:
        if slug == "*":
            if "*" not in ctx.board_scope:
                raise PermissionDeniedError("Token scope does not allow wildcard board subscriptions")
        else:
            require_board_role(conn, ctx, slug, "readonly", allow_archived=True)


def get_cursor_status(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
) -> dict[str, Any]:
    """Return the caller's read cursor, board latest_seq, and unread count for a board."""
    rconn = services.db.read_conn()
    board, _ = require_board_role(rconn, ctx, board_slug, "readonly", allow_archived=True)
    latest_seq = int(board["next_seq"]) - 1
    row = delivery_store.get_cursor_row(rconn, ctx.principal_id, board["id"])
    read_seq = int(row["read_seq"]) if row else 0
    updated_at = row["updated_at"] if row else None
    unread = delivery_store.count_unread_for_principal(rconn, board["id"], ctx.principal_id, read_seq)
    return {
        "board": board["slug"],
        "read_seq": read_seq,
        "latest_seq": latest_seq,
        "unread_count": unread,
        "updated_at": updated_at,
    }


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

    flt_raw = json.loads(sub_row["filter"]) if sub_row["filter"] else {}
    flt: dict[str, Any] = flt_raw if isinstance(flt_raw, dict) else {}
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
    if isinstance(allowed_types, list) and len(allowed_types) > 0:
        if msg_type not in allowed_types:
            return False

    return True


def format_dnd_status(principal_row: sqlite3.Row, *, now_iso: str | None = None) -> dict[str, Any]:
    """Return active DND state for a principal row (auto-expiring once past dnd_until)."""
    keys = set(principal_row.keys())
    until = principal_row["dnd_until"] if "dnd_until" in keys else None
    reason = principal_row["dnd_reason"] if "dnd_reason" in keys else None
    ref_iso = now_iso or utc_now_iso()
    active = bool(until and str(until) > ref_iso)
    return {
        "active": active,
        "until": str(until) if active else None,
        "reason": str(reason) if active and reason else None,
    }


def set_dnd(
    services: AppServices,
    ctx: AuthContext,
    *,
    enabled: bool = True,
    duration_minutes: int | None = None,
    reason: str | None = None,
    principal_handle: str | None = None,
) -> dict[str, Any]:
    """Enable finite Do-Not-Disturb wake deferral or clear it and expedite pending wakes."""
    target_handle = principal_handle.strip().lstrip("@").lower() if principal_handle else ctx.handle
    if target_handle != ctx.handle:
        if not ctx.has_server_owner_privilege:
            raise PermissionDeniedError("Only the server owner can manage another principal's DND")
    elif not ctx.has_server_owner_privilege and ctx.max_role == "readonly":
        raise PermissionDeniedError("Readonly tokens cannot modify DND status")

    if enabled:
        mins = DEFAULT_DND_MINUTES if duration_minutes is None else int(duration_minutes)
        if mins < 1 or mins > MAX_DND_MINUTES:
            raise ValidationError(f"duration_minutes must be between 1 and {MAX_DND_MINUTES}")
        raw_reason = (reason or "").strip()
        clean_reason: str | None = None
        if raw_reason:
            scrubbed, _ = services.redactor.scrub_text(raw_reason)
            clean_reason = " ".join(scrubbed.split())[:140] or None
    else:
        mins = 0
        clean_reason = None

    with services.db.write_tx() as conn:
        target = principals_store.get_principal_by_handle(conn, target_handle)
        if target is None or target["kind"] == "system":
            raise NotFoundError(f"Principal '{target_handle}' not found")
        now_dt = utc_now()
        now_iso = dt_to_iso(now_dt)
        expedited = 0
        if enabled:
            dnd_until: str | None = dt_to_iso(now_dt + timedelta(minutes=mins))
            principals_store.update_principal_dnd(
                conn, target["id"], dnd_until=dnd_until, dnd_reason=clean_reason
            )
        else:
            dnd_until = None
            principals_store.update_principal_dnd(conn, target["id"], dnd_until=None, dnd_reason=None)
            expedited = delivery_store.expedite_pending_outbox_for_principal(
                conn, principal_id=target["id"], now_iso=now_iso
            )
        record_audit(
            conn,
            ctx=ctx,
            action="principal.dnd",
            ok=True,
            target=target["handle"],
            detail={"enabled": enabled, "dnd_until": dnd_until, "reason": clean_reason, "expedited": expedited},
        )
        return {
            "handle": target["handle"],
            "dnd_active": enabled,
            "dnd_until": dnd_until,
            "dnd_reason": clean_reason,
            "expedited_outbox": expedited,
        }


def enqueue_outbox_for_message(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    board_slug: str,
    seq: int,
    author_id: str,
    msg_type: str,
    priority: str = "normal",
    mentioned_principal_ids: set[str],
    is_broadcast: bool = False,
    is_directed: bool = False,
    now_iso: str,
) -> None:
    """Enqueue or coalesce outbox nudges inside the caller's write transaction."""
    now_dt = iso_to_dt(now_iso)
    is_urgent = priority == "urgent"
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
        if is_urgent:
            next_attempt = now_dt
        else:
            debounce_dt = now_dt + timedelta(seconds=int(sub["debounce_s"]))
            next_attempt = debounce_dt
            if sub["last_sent_at"]:
                interval_dt = iso_to_dt(sub["last_sent_at"]) + timedelta(seconds=int(sub["min_interval_s"]))
                next_attempt = max(next_attempt, interval_dt)
            if int(sub["failure_count"]) > 0 and sub["updated_at"]:
                reprobe_floor_s = max(int(sub["min_interval_s"]), 300)
                reprobe_dt = iso_to_dt(sub["updated_at"]) + timedelta(seconds=reprobe_floor_s)
                next_attempt = max(next_attempt, reprobe_dt)
            dnd_until_raw = sub["principal_dnd_until"] if "principal_dnd_until" in sub.keys() else None
            if dnd_until_raw:
                dnd_dt = iso_to_dt(str(dnd_until_raw))
                if dnd_dt > next_attempt:
                    next_attempt = dnd_dt

        delivery_store.enqueue_outbox_pending(
            conn,
            outbox_id=new_ulid(),
            subscription_id=sub["id"],
            board_id=board_id,
            latest_seq=seq,
            next_attempt_at=dt_to_iso(next_attempt),
            created_at=now_iso,
            urgent=is_urgent,
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
    if min_interval_s < 0 or min_interval_s > MAX_SUB_INTERVAL_S:
        raise ValidationError(f"min_interval_s must be between 0 and {MAX_SUB_INTERVAL_S}")
    if debounce_s < 0 or debounce_s > MAX_SUB_INTERVAL_S:
        raise ValidationError(f"debounce_s must be between 0 and {MAX_SUB_INTERVAL_S}")

    target_principal_id = ctx.principal_id
    if principal_handle and principal_handle != ctx.handle:
        if not ctx.has_server_owner_privilege:
            raise PermissionDeniedError("Only the server owner can manage other principals' subscriptions")
        from agent_board.store import principals as principals_store

        p = principals_store.get_principal_by_handle(services.db.read_conn(), principal_handle)
        if p is None or p["kind"] == "system":
            raise NotFoundError(f"Principal '{principal_handle}' not found")
        target_principal_id = p["id"]
    elif not ctx.has_server_owner_privilege:
        if ctx.max_role == "readonly":
            raise PermissionDeniedError("Readonly tokens cannot manage subscriptions")

    clean_boards = [b.strip() for b in (boards or ["*"]) if b.strip()] or ["*"]
    clean_filter = _validate_filter_cfg(filter_cfg)

    clean_auth_header = normalize_auth_header(auth_header)
    secret_plain = hmac_secret or f"whsec_{secrets.token_urlsafe(32)}"
    audit_url, _ = services.redactor.scrub_text(clean_url)

    with services.db.write_tx() as conn:
        _validate_boards_for_caller(conn, ctx, clean_boards)

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
                detail={"url": audit_url, "boards": clean_boards},
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
    if all_principals and ctx.has_server_owner_privilege:
        rows = delivery_store.list_all_subscriptions(conn)
    else:
        rows = delivery_store.list_subscriptions_for_principal(conn, ctx.principal_id)
    return [subscription_to_dict(r) for r in rows]


def delete_subscription(services: AppServices, ctx: AuthContext, *, sub_id: str) -> dict[str, Any]:
    if not ctx.has_server_owner_privilege and ctx.max_role == "readonly":
        raise PermissionDeniedError("Readonly tokens cannot manage subscriptions")
    with services.db.write_tx() as conn:
        row = delivery_store.get_subscription_by_id(conn, sub_id)
        if row is None:
            raise NotFoundError(f"Subscription '{sub_id}' not found")
        if row["principal_id"] != ctx.principal_id and not ctx.has_server_owner_privilege:
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
    if not ctx.has_server_owner_privilege and ctx.max_role == "readonly":
        raise PermissionDeniedError("Readonly tokens cannot manage subscriptions")
    if min_interval_s is not None and (min_interval_s < 0 or min_interval_s > MAX_SUB_INTERVAL_S):
        raise ValidationError(f"min_interval_s must be between 0 and {MAX_SUB_INTERVAL_S}")
    if debounce_s is not None and (debounce_s < 0 or debounce_s > MAX_SUB_INTERVAL_S):
        raise ValidationError(f"debounce_s must be between 0 and {MAX_SUB_INTERVAL_S}")
    clean_url_input = _validate_webhook_url(url) if url is not None else None

    with services.db.write_tx() as conn:
        row = delivery_store.get_subscription_by_id(conn, sub_id)
        if row is None:
            raise NotFoundError(f"Subscription '{sub_id}' not found")
        if row["principal_id"] != ctx.principal_id and not ctx.has_server_owner_privilege:
            raise PermissionDeniedError("Cannot update another principal's subscription")

        next_url = clean_url_input if clean_url_input is not None else str(row["url"])
        next_boards = (
            [b.strip() for b in boards if b.strip()] or ["*"]
            if boards is not None
            else [str(b) for b in json.loads(row["boards"])]
        )
        if boards is not None:
            _validate_boards_for_caller(conn, ctx, next_boards)
        merged_filter = dict(json.loads(row["filter"]))
        if filter_cfg is not None:
            if not isinstance(filter_cfg, dict):
                raise ValidationError("Subscription filter must be a JSON object")
            for k, v in filter_cfg.items():
                if v is None:
                    merged_filter.pop(k, None)
                else:
                    merged_filter[k] = v
        next_filter = _validate_filter_cfg(merged_filter)

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
        audit_url, _ = services.redactor.scrub_text(next_url)
        record_audit(
            conn,
            ctx=ctx,
            action="subscription.update",
            ok=True,
            target=sub_id,
            detail={"url": audit_url, "boards": next_boards},
        )
        updated = delivery_store.get_subscription_by_id(conn, sub_id)
        return subscription_to_dict(updated)


def reenable_subscription(services: AppServices, ctx: AuthContext, *, sub_id: str) -> dict[str, Any]:
    if not ctx.has_server_owner_privilege and ctx.max_role == "readonly":
        raise PermissionDeniedError("Readonly tokens cannot manage subscriptions")
    with services.db.write_tx() as conn:
        row = delivery_store.get_subscription_by_id(conn, sub_id)
        if row is None:
            raise NotFoundError(f"Subscription '{sub_id}' not found")
        if row["principal_id"] != ctx.principal_id and not ctx.has_server_owner_privilege:
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
