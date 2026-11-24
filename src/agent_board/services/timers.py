"""Domain service for durable agent follow-up timers."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_board.errors import ConflictError, DomainError, NotFoundError, PermissionDeniedError, ValidationError
from agent_board.ids import dt_to_iso, new_ulid, utc_now, utc_now_iso
from agent_board.services.auth import record_audit, require_board_role
from agent_board.services.context import AppServices, AuthContext, system_context
from agent_board.services.messages import post_message_in_tx
from agent_board.store import boards as boards_store
from agent_board.store import timers as timers_store

logger = logging.getLogger(__name__)

MAX_DELAY_MINUTES = 10_080  # 7 days
MAX_DUE_AHEAD_DAYS = 30
MAX_PENDING_PER_PRINCIPAL = 25
MAX_TIMER_MESSAGE_CHARS = 500
VALID_TIMER_STATUSES = frozenset({"pending", "fired", "cancelled", "all"})


def _serialize_timer(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "board_slug": row["board_slug"],
        "principal_handle": row["principal_handle"],
        "thread_id": row["thread_id"],
        "thread_title": row["thread_title"],
        "due_at": row["due_at"],
        "message": row["message"],
        "cancel_on_reply": bool(row["cancel_on_reply"]),
        "baseline_seq": int(row["baseline_seq"]),
        "status": row["status"],
        "fired_message_id": row["fired_message_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_timer(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    message: str,
    delay_minutes: int | None = None,
    due_at: str | None = None,
    thread_id: str | None = None,
    cancel_on_reply: bool = False,
) -> dict[str, Any]:
    """Schedule a durable one-shot follow-up reminder for the caller on a board."""
    raw_msg = (message or "").strip()
    if not raw_msg:
        raise ValidationError("message must not be empty")
    scrubbed_msg, _ = services.redactor.scrub_text(raw_msg)
    clean_msg = " ".join(scrubbed_msg.split())[:MAX_TIMER_MESSAGE_CHARS].strip()
    if not clean_msg:
        raise ValidationError("message must not be empty")

    if (delay_minutes is None) == (due_at is None):
        raise ValidationError("Specify exactly one of delay_minutes or due_at")

    now_dt = utc_now()
    now_iso = dt_to_iso(now_dt)
    if delay_minutes is not None:
        mins = int(delay_minutes)
        if mins < 1 or mins > MAX_DELAY_MINUTES:
            raise ValidationError(f"delay_minutes must be between 1 and {MAX_DELAY_MINUTES}")
        due_dt = now_dt + timedelta(minutes=mins)
    else:
        assert due_at is not None
        raw_due = due_at.strip()
        try:
            parsed = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
        except Exception as exc:
            raise ValidationError("due_at must be a valid ISO-8601 timestamp with timezone") from exc
        if parsed.tzinfo is None:
            raise ValidationError("due_at must include a timezone offset or 'Z' (UTC)")
        due_dt = parsed.astimezone(UTC)
        if due_dt <= now_dt:
            raise ValidationError("due_at must be in the future")
        if due_dt > now_dt + timedelta(days=MAX_DUE_AHEAD_DAYS):
            raise ValidationError(f"due_at cannot be more than {MAX_DUE_AHEAD_DAYS} days in the future")
    due_iso = dt_to_iso(due_dt)

    if cancel_on_reply and not (thread_id and thread_id.strip()):
        raise ValidationError("cancel_on_reply requires thread_id")

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent")
        if timers_store.count_pending_timers_for_principal(conn, ctx.principal_id) >= MAX_PENDING_PER_PRINCIPAL:
            raise ValidationError(
                f"Cannot exceed {MAX_PENDING_PER_PRINCIPAL} pending timers per principal",
                code="timer_limit_exceeded",
            )

        resolved_thread_id: str | None = None
        if thread_id and thread_id.strip():
            clean_tid = thread_id.strip()
            thread = boards_store.get_thread_by_id(conn, clean_tid)
            if thread is None or thread["board_id"] != board["id"]:
                thread = boards_store.get_open_thread_by_title(conn, board["id"], clean_tid)
                if thread is None:
                    thread = boards_store.get_thread_by_title(conn, board["id"], clean_tid)
            if thread is None:
                raise NotFoundError(f"Thread '{thread_id}' not found on board '{board_slug}'")
            if thread["closed_at"] is not None:
                raise ValidationError("Thread is closed and read-only", code="thread_closed")
            resolved_thread_id = str(thread["id"])

        timer_id = new_ulid()
        baseline_seq = max(0, int(board["next_seq"]) - 1)
        timers_store.insert_timer(
            conn,
            timer_id=timer_id,
            board_id=board["id"],
            principal_id=ctx.principal_id,
            thread_id=resolved_thread_id,
            due_at=due_iso,
            message=clean_msg,
            cancel_on_reply=cancel_on_reply,
            baseline_seq=baseline_seq,
            now_iso=now_iso,
        )
        record_audit(
            conn,
            ctx=ctx,
            action="timer.create",
            ok=True,
            board_id=board["id"],
            target=timer_id,
            detail={"due_at": due_iso, "thread_id": resolved_thread_id, "cancel_on_reply": cancel_on_reply},
        )
        row = timers_store.get_timer_by_id(conn, timer_id)
        assert row is not None
        return _serialize_timer(row)


def list_timers(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    status: str = "pending",
    all_principals: bool = False,
) -> list[dict[str, Any]]:
    clean_status = (status or "pending").strip().lower()
    if clean_status not in VALID_TIMER_STATUSES:
        raise ValidationError("status must be one of: pending, fired, cancelled, all")
    with services.db.read_snapshot() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
        filter_pid = None if (all_principals and ctx.has_server_owner_privilege) else ctx.principal_id
        rows = timers_store.list_timers_for_board(
            conn,
            board_id=board["id"],
            principal_id=filter_pid,
            status=clean_status,
        )
        return [_serialize_timer(r) for r in rows]


def cancel_timer(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    timer_id: str,
) -> dict[str, Any]:
    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent", allow_archived=True)
        row = timers_store.get_timer_by_id(conn, timer_id)
        if row is None or row["board_id"] != board["id"]:
            raise NotFoundError(f"Timer '{timer_id}' not found on board '{board_slug}'")
        if row["principal_id"] != ctx.principal_id and not ctx.has_server_owner_privilege:
            raise PermissionDeniedError("Only the timer owner or server owner can cancel a timer")
        if row["status"] == "fired":
            raise ConflictError("Timer has already fired", code="timer_already_fired")
        now_iso = utc_now_iso()
        changed = timers_store.mark_timer_cancelled(conn, timer_id=timer_id, now_iso=now_iso)
        if changed:
            record_audit(
                conn,
                ctx=ctx,
                action="timer.cancel",
                ok=True,
                board_id=board["id"],
                target=timer_id,
            )
        updated = timers_store.get_timer_by_id(conn, timer_id)
        assert updated is not None
        return _serialize_timer(updated)


def _resolve_open_reminders_thread_id(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    created_by: str,
    now_iso: str,
) -> str:
    open_thr = boards_store.get_open_thread_by_title(conn, board_id, "reminders")
    if open_thr is not None:
        return str(open_thr["id"])
    new_tid = new_ulid()
    boards_store.insert_thread(
        conn,
        thread_id=new_tid,
        board_id=board_id,
        title="reminders",
        created_by=created_by,
        created_at=now_iso,
    )
    return new_tid


def process_due_timers(services: AppServices, *, now: datetime | None = None) -> int:
    """Fire due pending timers in isolated transactions (or auto-cancel on reply / invalid target)."""
    current = now or utc_now()
    now_iso = dt_to_iso(current)
    processed = 0
    sys_ctx = system_context()

    with services.db.read_snapshot() as rconn:
        due_rows = timers_store.list_due_timers(rconn, now_iso=now_iso, limit=20)

    for due_row in due_rows:
        processed += 1
        timer_id = str(due_row["id"])
        with services.db.write_tx() as conn:
            row = timers_store.get_timer_for_firing(conn, timer_id)
            if row is None or row["status"] != "pending":
                continue

            has_board_access = bool(row["principal_is_server_owner"]) or bool(row["is_board_member"])
            if (
                row["board_archived_at"] is not None
                or row["principal_disabled_at"] is not None
                or not has_board_access
            ):
                timers_store.mark_timer_cancelled(conn, timer_id=timer_id, now_iso=now_iso)
                continue

            if bool(row["cancel_on_reply"]) and row["thread_id"] is not None:
                if timers_store.has_other_reply_in_thread_since(
                    conn,
                    board_id=row["board_id"],
                    thread_id=row["thread_id"],
                    principal_id=row["principal_id"],
                    baseline_seq=int(row["baseline_seq"]),
                ):
                    timers_store.mark_timer_cancelled(conn, timer_id=timer_id, now_iso=now_iso)
                    continue

            board = boards_store.get_board_by_id(conn, row["board_id"])
            if board is None:
                timers_store.mark_timer_cancelled(conn, timer_id=timer_id, now_iso=now_iso)
                continue

            if row["thread_id"] is not None and row["thread_closed_at"] is None:
                target_thread_id = str(row["thread_id"])
            else:
                target_thread_id = _resolve_open_reminders_thread_id(
                    conn,
                    board_id=str(row["board_id"]),
                    created_by=sys_ctx.principal_id,
                    now_iso=now_iso,
                )

            handle = str(row["principal_handle"])
            body_md = f"@{handle} [Follow-up timer `{timer_id}`] {row['message']}"
            try:
                msg = post_message_in_tx(
                    conn,
                    services=services,
                    ctx=sys_ctx,
                    board=board,
                    msg_type="system",
                    body_md=body_md,
                    thread_id=target_thread_id,
                    to=[handle],
                    data={"timer_id": timer_id, "principal": handle},
                    now_iso=now_iso,
                )
            except DomainError as exc:
                logger.warning(
                    "Auto-cancelling unfireable timer %s on %s: %s",
                    timer_id,
                    row["board_slug"],
                    exc.message,
                )
                timers_store.mark_timer_cancelled(conn, timer_id=timer_id, now_iso=now_iso)
                continue

            timers_store.mark_timer_fired(
                conn,
                timer_id=timer_id,
                fired_message_id=str(msg["id"]),
                now_iso=now_iso,
            )
    return processed
