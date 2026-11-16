"""SQL helpers for boards, memberships, and threads."""

from __future__ import annotations

import sqlite3


def insert_board(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    slug: str,
    name: str,
    purpose: str,
    settings_json: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO boards (id, slug, name, purpose, settings_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (board_id, slug, name, purpose, settings_json, created_at),
    )


def get_board_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM boards WHERE slug = ?", (slug,)).fetchone()


def get_board_by_id(conn: sqlite3.Connection, board_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()


def list_boards_for_principal(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    include_archived: bool = False,
    is_server_owner: bool = False,
) -> list[sqlite3.Row]:
    archive_clause = "" if include_archived else "AND b.archived_at IS NULL"
    role_select = "COALESCE(m.role, 'owner') AS membership_role" if is_server_owner else "m.role AS membership_role"
    membership_join = (
        "LEFT JOIN memberships m ON m.board_id = b.id AND m.principal_id = ?"
        if is_server_owner
        else "JOIN memberships m ON m.board_id = b.id AND m.principal_id = ?"
    )
    return conn.execute(
        f"""
        SELECT b.*, {role_select},
               COALESCE(c.read_seq, 0) AS read_seq,
               (SELECT COUNT(*)
                FROM messages msg
                WHERE msg.board_id = b.id
                  AND msg.seq > COALESCE(c.read_seq, 0)
                  AND msg.author_id != ?) AS unread_count,
               (SELECT COUNT(*)
                FROM issues i
                WHERE i.board_id = b.id
                  AND i.status IN ('open', 'in_progress')) AS open_issues_count,
               (SELECT COUNT(*)
                FROM decisions d
                WHERE d.board_id = b.id
                  AND d.status = 'active') AS active_decisions_count,
               (SELECT MAX(msg.created_at)
                FROM messages msg
                WHERE msg.board_id = b.id) AS last_activity_at
        FROM boards b
        {membership_join}
        LEFT JOIN cursors c ON c.board_id = b.id AND c.principal_id = ?
        WHERE 1 = 1 {archive_clause}
        ORDER BY COALESCE(last_activity_at, b.created_at) DESC, b.slug ASC
        """,
        (principal_id, principal_id, principal_id),
    ).fetchall()


def list_all_boards(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM boards ORDER BY slug ASC").fetchall()


def get_board_stats_for_principal(conn: sqlite3.Connection, board_id: str, principal_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT b.*,
               m.role AS membership_role,
               COALESCE(c.read_seq, 0) AS read_seq,
               (SELECT COUNT(*)
                FROM messages msg
                WHERE msg.board_id = b.id
                  AND msg.seq > COALESCE(c.read_seq, 0)
                  AND msg.author_id != ?) AS unread_count,
               (SELECT COUNT(*)
                FROM issues i
                WHERE i.board_id = b.id
                  AND i.status IN ('open', 'in_progress')) AS open_issues_count,
               (SELECT COUNT(*)
                FROM decisions d
                WHERE d.board_id = b.id
                  AND d.status = 'active') AS active_decisions_count,
               (SELECT MAX(msg.created_at)
                FROM messages msg
                WHERE msg.board_id = b.id) AS last_activity_at
        FROM boards b
        LEFT JOIN memberships m ON m.board_id = b.id AND m.principal_id = ?
        LEFT JOIN cursors c ON c.board_id = b.id AND c.principal_id = ?
        WHERE b.id = ?
        """,
        (principal_id, principal_id, principal_id, board_id),
    ).fetchone()


def update_board(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    name: str,
    purpose: str,
    settings_json: str,
    archived_at: str | None,
) -> None:
    conn.execute(
        """
        UPDATE boards
        SET name = ?, purpose = ?, settings_json = ?, archived_at = ?
        WHERE id = ?
        """,
        (name, purpose, settings_json, archived_at, board_id),
    )


def allocate_next_seq(conn: sqlite3.Connection, board_id: str) -> int:
    row = conn.execute(
        "UPDATE boards SET next_seq = next_seq + 1 WHERE id = ? RETURNING next_seq - 1 AS seq",
        (board_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Board {board_id} disappeared during seq allocation")
    return int(row["seq"])


def allocate_next_decision_num(conn: sqlite3.Connection, board_id: str) -> int:
    row = conn.execute(
        """
        UPDATE boards
        SET next_decision_num = next_decision_num + 1
        WHERE id = ?
        RETURNING next_decision_num - 1 AS num
        """,
        (board_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Board {board_id} disappeared during decision allocation")
    return int(row["num"])


def allocate_next_issue_num(conn: sqlite3.Connection, board_id: str) -> int:
    row = conn.execute(
        """
        UPDATE boards
        SET next_issue_num = next_issue_num + 1
        WHERE id = ?
        RETURNING next_issue_num - 1 AS num
        """,
        (board_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Board {board_id} disappeared during issue allocation")
    return int(row["num"])


def upsert_membership(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    principal_id: str,
    role: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO memberships (board_id, principal_id, role, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(board_id, principal_id) DO UPDATE SET role = excluded.role
        """,
        (board_id, principal_id, role, created_at),
    )


def get_membership(conn: sqlite3.Connection, board_id: str, principal_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM memberships WHERE board_id = ? AND principal_id = ?",
        (board_id, principal_id),
    ).fetchone()


def list_memberships_for_board(conn: sqlite3.Connection, board_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.*, p.handle, p.display_name, p.kind
        FROM memberships m
        JOIN principals p ON p.id = m.principal_id
        WHERE m.board_id = ?
        ORDER BY p.handle
        """,
        (board_id,),
    ).fetchall()


def list_all_memberships(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.*, b.slug AS board_slug, p.handle AS principal_handle
        FROM memberships m
        JOIN boards b ON b.id = m.board_id
        JOIN principals p ON p.id = m.principal_id
        ORDER BY b.slug, p.handle
        """
    ).fetchall()


def delete_membership(conn: sqlite3.Connection, board_id: str, principal_id: str) -> int:
    cur = conn.execute(
        "DELETE FROM memberships WHERE board_id = ? AND principal_id = ?",
        (board_id, principal_id),
    )
    return cur.rowcount


def insert_thread(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    board_id: str,
    title: str,
    created_by: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO threads (id, board_id, title, created_by, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (thread_id, board_id, title, created_by, created_at),
    )


def get_thread_by_id(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT t.*, p.handle AS created_by_handle,
               (SELECT COUNT(DISTINCT m.root_id) FROM messages m WHERE m.thread_id = t.id) AS message_count,
               COALESCE((SELECT MAX(m.seq) FROM messages m WHERE m.thread_id = t.id), 0) AS last_seq,
               (SELECT MAX(m.created_at) FROM messages m WHERE m.thread_id = t.id) AS last_activity_at
        FROM threads t
        JOIN principals p ON p.id = t.created_by
        WHERE t.id = ?
        """,
        (thread_id,),
    ).fetchone()


def get_open_thread_by_title(conn: sqlite3.Connection, board_id: str, title: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT t.*, p.handle AS created_by_handle,
               (SELECT COUNT(DISTINCT m.root_id) FROM messages m WHERE m.thread_id = t.id) AS message_count,
               COALESCE((SELECT MAX(m.seq) FROM messages m WHERE m.thread_id = t.id), 0) AS last_seq,
               (SELECT MAX(m.created_at) FROM messages m WHERE m.thread_id = t.id) AS last_activity_at
        FROM threads t
        JOIN principals p ON p.id = t.created_by
        WHERE t.board_id = ? AND LOWER(t.title) = LOWER(?) AND t.closed_at IS NULL
        LIMIT 1
        """,
        (board_id, title),
    ).fetchone()


def get_thread_by_title(conn: sqlite3.Connection, board_id: str, title: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT t.*, p.handle AS created_by_handle,
               (SELECT COUNT(DISTINCT m.root_id) FROM messages m WHERE m.thread_id = t.id) AS message_count,
               COALESCE((SELECT MAX(m.seq) FROM messages m WHERE m.thread_id = t.id), 0) AS last_seq,
               (SELECT MAX(m.created_at) FROM messages m WHERE m.thread_id = t.id) AS last_activity_at
        FROM threads t
        JOIN principals p ON p.id = t.created_by
        WHERE t.board_id = ? AND LOWER(t.title) = LOWER(?)
        ORDER BY t.created_at DESC
        LIMIT 1
        """,
        (board_id, title),
    ).fetchone()


def list_threads(conn: sqlite3.Connection, board_id: str, *, state: str = "open") -> list[sqlite3.Row]:
    where_state = ""
    if state == "open":
        where_state = "AND t.closed_at IS NULL"
    elif state == "closed":
        where_state = "AND t.closed_at IS NOT NULL"
    return conn.execute(
        f"""
        SELECT t.*, p.handle AS created_by_handle,
               (SELECT COUNT(DISTINCT m.root_id) FROM messages m WHERE m.thread_id = t.id) AS message_count,
               COALESCE((SELECT MAX(m.seq) FROM messages m WHERE m.thread_id = t.id), 0) AS last_seq,
               (SELECT MAX(m.created_at) FROM messages m WHERE m.thread_id = t.id) AS last_activity_at
        FROM threads t
        JOIN principals p ON p.id = t.created_by
        WHERE t.board_id = ? {where_state}
        ORDER BY COALESCE(last_activity_at, t.created_at) DESC, t.created_at DESC
        """,
        (board_id,),
    ).fetchall()


def set_thread_closed(conn: sqlite3.Connection, thread_id: str, closed_at: str | None) -> None:
    conn.execute("UPDATE threads SET closed_at = ? WHERE id = ?", (closed_at, thread_id))
