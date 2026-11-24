"""SQL helpers for durable agent follow-up timers."""

from __future__ import annotations

import sqlite3


def insert_timer(
    conn: sqlite3.Connection,
    *,
    timer_id: str,
    board_id: str,
    principal_id: str,
    thread_id: str | None,
    due_at: str,
    message: str,
    cancel_on_reply: bool,
    baseline_seq: int,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO timers (
            id, board_id, principal_id, thread_id, due_at, message,
            cancel_on_reply, baseline_seq, status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            timer_id,
            board_id,
            principal_id,
            thread_id,
            due_at,
            message,
            int(cancel_on_reply),
            baseline_seq,
            now_iso,
            now_iso,
        ),
    )


def count_pending_timers_for_principal(conn: sqlite3.Connection, principal_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM timers WHERE principal_id = ? AND status = 'pending'",
        (principal_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


def get_timer_by_id(conn: sqlite3.Connection, timer_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT tm.*, b.slug AS board_slug, p.handle AS principal_handle, t.title AS thread_title
        FROM timers tm
        JOIN boards b ON b.id = tm.board_id
        JOIN principals p ON p.id = tm.principal_id
        LEFT JOIN threads t ON t.id = tm.thread_id
        WHERE tm.id = ?
        """,
        (timer_id,),
    ).fetchone()


def list_timers_for_board(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    principal_id: str | None = None,
    status: str = "pending",
    limit: int = 100,
) -> list[sqlite3.Row]:
    clauses = ["tm.board_id = ?"]
    params: list[object] = [board_id]
    if principal_id is not None:
        clauses.append("tm.principal_id = ?")
        params.append(principal_id)
    if status != "all":
        clauses.append("tm.status = ?")
        params.append(status)
    where_sql = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT tm.*, b.slug AS board_slug, p.handle AS principal_handle, t.title AS thread_title
        FROM timers tm
        JOIN boards b ON b.id = tm.board_id
        JOIN principals p ON p.id = tm.principal_id
        LEFT JOIN threads t ON t.id = tm.thread_id
        WHERE {where_sql}
        ORDER BY tm.due_at ASC, tm.created_at ASC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()


def list_due_timers(conn: sqlite3.Connection, *, now_iso: str, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT tm.*, b.slug AS board_slug, b.archived_at AS board_archived_at,
               p.handle AS principal_handle, p.disabled_at AS principal_disabled_at,
               p.is_server_owner AS principal_is_server_owner,
               EXISTS(
                   SELECT 1 FROM memberships mb
                   WHERE mb.board_id = tm.board_id AND mb.principal_id = tm.principal_id
               ) AS is_board_member,
               t.title AS thread_title, t.closed_at AS thread_closed_at
        FROM timers tm
        JOIN boards b ON b.id = tm.board_id
        JOIN principals p ON p.id = tm.principal_id
        LEFT JOIN threads t ON t.id = tm.thread_id
        WHERE tm.status = 'pending' AND tm.due_at <= ?
        ORDER BY tm.due_at ASC, tm.created_at ASC
        LIMIT ?
        """,
        (now_iso, limit),
    ).fetchall()


def has_other_reply_in_thread_since(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    thread_id: str,
    principal_id: str,
    baseline_seq: int,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM messages m
        JOIN principals p ON p.id = m.author_id
        WHERE m.board_id = ?
          AND m.thread_id = ?
          AND m.seq > ?
          AND m.author_id != ?
          AND m.type != 'system'
          AND p.kind != 'system'
          AND m.redacted_at IS NULL
        LIMIT 1
        """,
        (board_id, thread_id, baseline_seq, principal_id),
    ).fetchone()
    return row is not None


def mark_timer_fired(
    conn: sqlite3.Connection,
    *,
    timer_id: str,
    fired_message_id: str,
    now_iso: str,
) -> None:
    conn.execute(
        """
        UPDATE timers
        SET status = 'fired', fired_message_id = ?, updated_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (fired_message_id, now_iso, timer_id),
    )


def mark_timer_cancelled(
    conn: sqlite3.Connection,
    *,
    timer_id: str,
    now_iso: str,
) -> int:
    cur = conn.execute(
        """
        UPDATE timers
        SET status = 'cancelled', updated_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (now_iso, timer_id),
    )
    return cur.rowcount
