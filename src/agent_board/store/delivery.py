"""SQL helpers for cursors, subscriptions, and transactional outbox."""

from __future__ import annotations

import sqlite3


def get_cursor(conn: sqlite3.Connection, principal_id: str, board_id: str) -> int:
    row = get_cursor_row(conn, principal_id, board_id)
    return int(row["read_seq"]) if row else 0


def get_cursor_row(conn: sqlite3.Connection, principal_id: str, board_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT read_seq, updated_at FROM cursors WHERE principal_id = ? AND board_id = ?",
        (principal_id, board_id),
    ).fetchone()


def list_cursors_for_board(conn: sqlite3.Connection, board_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT principal_id, read_seq, updated_at FROM cursors WHERE board_id = ?",
        (board_id,),
    ).fetchall()


def upsert_cursor(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    board_id: str,
    read_seq: int,
    updated_at: str,
) -> int:
    conn.execute(
        """
        INSERT INTO cursors (principal_id, board_id, read_seq, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(principal_id, board_id) DO UPDATE SET
            read_seq = MAX(cursors.read_seq, excluded.read_seq),
            updated_at = CASE
                WHEN excluded.read_seq > cursors.read_seq THEN excluded.updated_at
                ELSE cursors.updated_at
            END
        """,
        (principal_id, board_id, read_seq, updated_at),
    )
    return get_cursor(conn, principal_id, board_id)


def count_unread_for_principal(conn: sqlite3.Connection, board_id: str, principal_id: str, read_seq: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM messages
        WHERE board_id = ? AND seq > ? AND author_id != ?
        """,
        (board_id, read_seq, principal_id),
    ).fetchone()
    return int(row["cnt"]) if row else 0


def insert_subscription(
    conn: sqlite3.Connection,
    *,
    sub_id: str,
    principal_id: str,
    url: str,
    auth_header_enc: str | None,
    hmac_secret_enc: str,
    payload_format: str,
    boards_json: str,
    filter_json: str,
    min_interval_s: int,
    debounce_s: int,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO subscriptions (
            id, principal_id, url, auth_header_enc, hmac_secret_enc, payload_format,
            boards, filter, min_interval_s, debounce_s, active, failure_count,
            disabled_reason, last_sent_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, NULL, NULL, ?, ?)
        """,
        (
            sub_id,
            principal_id,
            url,
            auth_header_enc,
            hmac_secret_enc,
            payload_format,
            boards_json,
            filter_json,
            min_interval_s,
            debounce_s,
            now_iso,
            now_iso,
        ),
    )


def get_subscription_by_id(conn: sqlite3.Connection, sub_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT s.*, p.handle AS principal_handle, p.is_server_owner AS is_server_owner
        FROM subscriptions s
        JOIN principals p ON p.id = s.principal_id
        WHERE s.id = ?
        """,
        (sub_id,),
    ).fetchone()


def list_subscriptions_for_principal(conn: sqlite3.Connection, principal_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.*, p.handle AS principal_handle
        FROM subscriptions s
        JOIN principals p ON p.id = s.principal_id
        WHERE s.principal_id = ?
        ORDER BY s.created_at DESC
        """,
        (principal_id,),
    ).fetchall()


def list_all_subscriptions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.*, p.handle AS principal_handle
        FROM subscriptions s
        JOIN principals p ON p.id = s.principal_id
        ORDER BY p.handle, s.created_at DESC
        """
    ).fetchall()


def list_active_subscriptions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.*, p.disabled_at AS principal_disabled_at
        FROM subscriptions s
        JOIN principals p ON p.id = s.principal_id
        WHERE s.active = 1 AND p.disabled_at IS NULL
        """
    ).fetchall()


def list_active_subscriptions_for_board(conn: sqlite3.Connection, board_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.*, p.disabled_at AS principal_disabled_at
        FROM subscriptions s
        JOIN principals p ON p.id = s.principal_id
        WHERE s.active = 1
          AND p.disabled_at IS NULL
          AND (
              p.is_server_owner = 1
              OR EXISTS (
                  SELECT 1 FROM memberships m
                  WHERE m.board_id = ? AND m.principal_id = s.principal_id
              )
          )
        """,
        (board_id,),
    ).fetchall()


def list_subscriptions_for_board_members(conn: sqlite3.Connection, board_id: str) -> list[sqlite3.Row]:
    """Return non-secret subscription status fields for principals enrolled on a board."""
    return conn.execute(
        """
        SELECT s.id, s.principal_id, s.boards, s.active, s.failure_count, s.disabled_reason, s.last_sent_at
        FROM subscriptions s
        WHERE EXISTS (
            SELECT 1 FROM memberships m
            WHERE m.board_id = ? AND m.principal_id = s.principal_id
        )
        """,
        (board_id,),
    ).fetchall()


def delete_subscription(conn: sqlite3.Connection, sub_id: str) -> int:
    cur = conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    return cur.rowcount


def update_subscription(
    conn: sqlite3.Connection,
    *,
    sub_id: str,
    url: str,
    auth_header_enc: str | None,
    update_auth_header: bool,
    boards_json: str,
    filter_json: str,
    min_interval_s: int,
    debounce_s: int,
    now_iso: str,
) -> None:
    if update_auth_header:
        conn.execute(
            """
            UPDATE subscriptions
            SET url = ?, auth_header_enc = ?, boards = ?, filter = ?,
                min_interval_s = ?, debounce_s = ?, updated_at = ?
            WHERE id = ?
            """,
            (url, auth_header_enc, boards_json, filter_json, min_interval_s, debounce_s, now_iso, sub_id),
        )
        return
    conn.execute(
        """
        UPDATE subscriptions
        SET url = ?, boards = ?, filter = ?, min_interval_s = ?, debounce_s = ?, updated_at = ?
        WHERE id = ?
        """,
        (url, boards_json, filter_json, min_interval_s, debounce_s, now_iso, sub_id),
    )


def set_subscription_active(
    conn: sqlite3.Connection,
    *,
    sub_id: str,
    active: bool,
    disabled_reason: str | None,
    updated_at: str,
) -> None:
    conn.execute(
        """
        UPDATE subscriptions
        SET active = ?, failure_count = CASE WHEN ? = 1 THEN 0 ELSE failure_count END,
            disabled_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (int(active), int(active), disabled_reason, updated_at, sub_id),
    )


def enqueue_outbox_pending(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    subscription_id: str,
    board_id: str,
    latest_seq: int,
    next_attempt_at: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO outbox (
            id, subscription_id, board_id, latest_seq, attempt, next_attempt_at, status, created_at
        )
        VALUES (?, ?, ?, ?, 0, ?, 'pending', ?)
        ON CONFLICT(subscription_id, board_id) WHERE status = 'pending'
        DO UPDATE SET
            latest_seq = MAX(outbox.latest_seq, excluded.latest_seq),
            next_attempt_at = CASE
                WHEN excluded.latest_seq > outbox.latest_seq
                     AND outbox.attempt > 0
                     AND excluded.next_attempt_at < outbox.next_attempt_at
                THEN excluded.next_attempt_at
                ELSE outbox.next_attempt_at
            END
        """,
        (outbox_id, subscription_id, board_id, latest_seq, next_attempt_at, created_at),
    )


def heal_subscription_delivery(
    conn: sqlite3.Connection,
    *,
    sub_id: str,
    now_iso: str,
    force_enable: bool = False,
) -> None:
    """Reset failures, clear Auto-disabled state, suppress acked outbox rows, and expedite unread pending rows."""
    if force_enable:
        conn.execute(
            """
            UPDATE subscriptions
            SET active = 1, failure_count = 0, disabled_reason = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now_iso, sub_id),
        )
    else:
        conn.execute(
            """
            UPDATE subscriptions
            SET active = CASE
                    WHEN active = 0 AND disabled_reason LIKE 'Auto-disabled%' THEN 1
                    ELSE active
                END,
                failure_count = 0,
                disabled_reason = CASE
                    WHEN active = 0 AND disabled_reason LIKE 'Auto-disabled%' THEN NULL
                    ELSE disabled_reason
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (now_iso, sub_id),
        )

    conn.execute(
        """
        UPDATE outbox
        SET status = 'suppressed', locked_at = NULL, last_error = 'already_acked'
        WHERE subscription_id = ?
          AND status = 'pending'
          AND latest_seq <= COALESCE(
              (SELECT c.read_seq FROM cursors c
               JOIN subscriptions s ON s.principal_id = c.principal_id
               WHERE s.id = outbox.subscription_id AND c.board_id = outbox.board_id),
              0
          )
        """,
        (sub_id,),
    )
    conn.execute(
        """
        UPDATE outbox
        SET attempt = 0, next_attempt_at = ?
        WHERE subscription_id = ?
          AND status = 'pending'
          AND (attempt > 0 OR next_attempt_at > ?)
        """,
        (now_iso, sub_id, now_iso),
    )


def heal_principal_delivery_on_activity(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    now_iso: str,
    board_id: str | None = None,
    acked_seq: int | None = None,
) -> None:
    """Suppress acked pending outbox rows and reset transient webhook failure state on agent activity."""
    if principal_id == "00000000000000000000000000":
        return

    if board_id is not None and acked_seq is not None:
        conn.execute(
            """
            UPDATE outbox
            SET status = 'suppressed', locked_at = NULL, last_error = 'already_acked'
            WHERE board_id = ?
              AND status = 'pending'
              AND latest_seq <= ?
              AND subscription_id IN (SELECT id FROM subscriptions WHERE principal_id = ?)
            """,
            (board_id, acked_seq, principal_id),
        )

    conn.execute(
        """
        UPDATE subscriptions
        SET active = CASE
                WHEN active = 0 AND disabled_reason LIKE 'Auto-disabled%' THEN 1
                ELSE active
            END,
            failure_count = 0,
            disabled_reason = CASE
                WHEN active = 0 AND disabled_reason LIKE 'Auto-disabled%' THEN NULL
                ELSE disabled_reason
            END,
            updated_at = ?
        WHERE principal_id = ?
          AND (failure_count > 0 OR (active = 0 AND disabled_reason LIKE 'Auto-disabled%'))
        """,
        (now_iso, principal_id),
    )


def recover_stale_sending_outbox(conn: sqlite3.Connection, stale_before_iso: str) -> int:
    """Recover sending rows left behind by a crash without violating the one-pending unique index."""
    stale_rows = conn.execute(
        """
        SELECT id, subscription_id, board_id, latest_seq
        FROM outbox
        WHERE status = 'sending' AND locked_at < ?
        """,
        (stale_before_iso,),
    ).fetchall()
    recovered = 0
    for row in stale_rows:
        pending = conn.execute(
            """
            SELECT id FROM outbox
            WHERE subscription_id = ? AND board_id = ? AND status = 'pending'
            """,
            (row["subscription_id"], row["board_id"]),
        ).fetchone()
        if pending is not None:
            conn.execute(
                "UPDATE outbox SET latest_seq = MAX(latest_seq, ?) WHERE id = ?",
                (row["latest_seq"], pending["id"]),
            )
            conn.execute(
                """
                UPDATE outbox
                SET status = 'suppressed', locked_at = NULL, last_error = 'merged_stale_sending'
                WHERE id = ?
                """,
                (row["id"],),
            )
        else:
            conn.execute(
                "UPDATE outbox SET status = 'pending', locked_at = NULL WHERE id = ?",
                (row["id"],),
            )
        recovered += 1
    return recovered


def claim_due_outbox_rows(conn: sqlite3.Connection, *, now_iso: str, limit: int = 20) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT o.*, b.slug AS board_slug, s.principal_id, s.url, s.auth_header_enc,
               s.hmac_secret_enc, s.payload_format, s.filter, s.active, s.failure_count,
               p.handle AS principal_handle
        FROM outbox o
        JOIN boards b ON b.id = o.board_id
        JOIN subscriptions s ON s.id = o.subscription_id
        JOIN principals p ON p.id = s.principal_id
        WHERE o.status = 'pending' AND o.next_attempt_at <= ?
        ORDER BY o.next_attempt_at ASC, o.created_at ASC
        LIMIT ?
        """,
        (now_iso, limit),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE outbox SET status = 'sending', locked_at = ? WHERE id = ?",
            (now_iso, row["id"]),
        )
    return rows


def mark_outbox_sent(conn: sqlite3.Connection, *, outbox_id: str, subscription_id: str, now_iso: str) -> None:
    conn.execute(
        "UPDATE outbox SET status = 'sent', sent_at = ?, locked_at = NULL WHERE id = ?",
        (now_iso, outbox_id),
    )
    conn.execute(
        """
        UPDATE subscriptions
        SET failure_count = 0, last_sent_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (now_iso, now_iso, subscription_id),
    )


def mark_outbox_suppressed(conn: sqlite3.Connection, *, outbox_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE outbox SET status = 'suppressed', locked_at = NULL, last_error = ? WHERE id = ?",
        (reason, outbox_id),
    )


def record_outbox_failure(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    subscription_id: str,
    new_attempt: int,
    next_attempt_at: str,
    dead: bool,
    last_error: str,
    now_iso: str,
) -> tuple[int, bool]:
    status = "dead" if dead else "pending"
    if status == "pending":
        existing_pending = conn.execute(
            """
            SELECT o2.id
            FROM outbox o1
            JOIN outbox o2 ON o2.subscription_id = o1.subscription_id
                          AND o2.board_id = o1.board_id
                          AND o2.status = 'pending'
            WHERE o1.id = ?
            """,
            (outbox_id,),
        ).fetchone()
        if existing_pending is not None:
            # Newer pending row already queued while sending; fold latest_seq into it
            row = conn.execute("SELECT latest_seq FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
            conn.execute(
                "UPDATE outbox SET latest_seq = MAX(latest_seq, ?) WHERE id = ?",
                (row["latest_seq"], existing_pending["id"]),
            )
            conn.execute(
                """
                UPDATE outbox
                SET status = 'suppressed', locked_at = NULL, last_error = ?
                WHERE id = ?
                """,
                (f"superseded_after_fail: {last_error[:200]}", outbox_id),
            )
        else:
            conn.execute(
                """
                UPDATE outbox
                SET status = 'pending', attempt = ?, next_attempt_at = ?,
                    locked_at = NULL, last_error = ?
                WHERE id = ?
                """,
                (new_attempt, next_attempt_at, last_error[:500], outbox_id),
            )
    else:
        conn.execute(
            """
            UPDATE outbox
            SET status = 'dead', attempt = ?, locked_at = NULL, last_error = ?
            WHERE id = ?
            """,
            (new_attempt, last_error[:500], outbox_id),
        )

    sub_row = conn.execute(
        """
        UPDATE subscriptions
        SET failure_count = failure_count + 1, updated_at = ?
        WHERE id = ?
        RETURNING failure_count
        """,
        (now_iso, subscription_id),
    ).fetchone()
    failures = int(sub_row["failure_count"]) if sub_row else 0
    auto_disabled = False
    if failures >= 20:
        cur = conn.execute(
            """
            UPDATE subscriptions
            SET active = 0,
                disabled_reason = ?,
                updated_at = ?
            WHERE id = ? AND active = 1
            """,
            (f"Auto-disabled after {failures} consecutive failures: {last_error[:160]}", now_iso, subscription_id),
        )
        auto_disabled = cur.rowcount > 0
    return failures, auto_disabled


def get_unread_summary_for_nudge(
    conn: sqlite3.Connection, *, board_id: str, principal_id: str, read_seq: int
) -> tuple[int, list[str], bool]:
    rows = conn.execute(
        """
        SELECT m.id, m.type, m.author_id,
               EXISTS(
                   SELECT 1 FROM mentions mn
                   WHERE mn.message_id = m.id AND mn.principal_id = ?
               ) AS is_mentioned
        FROM messages m
        WHERE m.board_id = ? AND m.seq > ? AND m.author_id != ?
        ORDER BY m.seq ASC
        """,
        (principal_id, board_id, read_seq, principal_id),
    ).fetchall()
    unread_count = len(rows)
    seen_types: list[str] = []
    mentioned = False
    for row in rows:
        if row["type"] not in seen_types:
            seen_types.append(row["type"])
        if row["is_mentioned"]:
            mentioned = True
    return unread_count, seen_types, mentioned


def prune_outbox_before(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    cur = conn.execute(
        """
        DELETE FROM outbox
        WHERE status IN ('sent', 'suppressed', 'dead')
          AND created_at < ?
        """,
        (cutoff_iso,),
    )
    return cur.rowcount
