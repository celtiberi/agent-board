"""SQL helpers for issues, issue_events, and issues_fts."""

from __future__ import annotations

import sqlite3


def insert_issue(
    conn: sqlite3.Connection,
    *,
    issue_id: str,
    board_id: str,
    number: int,
    title: str,
    body_md: str,
    severity: str,
    assignee_id: str | None,
    opened_by: str,
    thread_id: str,
    tags_json: str,
    now_iso: str,
) -> int:
    conn.execute(
        """
        INSERT INTO issues (
            id, board_id, number, title, body_md, severity, status,
            assignee_id, opened_by, opened_at, resolved_at, resolution,
            thread_id, tags, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, NULL, NULL, ?, ?, ?)
        """,
        (
            issue_id,
            board_id,
            number,
            title,
            body_md,
            severity,
            assignee_id,
            opened_by,
            now_iso,
            thread_id,
            tags_json,
            now_iso,
        ),
    )
    row = conn.execute("SELECT rowid FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return int(row["rowid"])


def insert_issue_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    issue_id: str,
    action: str,
    from_val: str | None,
    to_val: str | None,
    actor_id: str,
    message_id: str,
    at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO issue_events (id, issue_id, action, from_val, to_val, actor_id, message_id, at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, issue_id, action, from_val, to_val, actor_id, message_id, at),
    )


def upsert_issue_fts(
    conn: sqlite3.Connection,
    *,
    issue_rowid: int,
    issue_id: str,
    board_id: str,
    title: str,
    body_md: str,
    resolution: str | None,
) -> None:
    conn.execute("DELETE FROM issues_fts WHERE rowid = ?", (issue_rowid,))
    conn.execute(
        """
        INSERT INTO issues_fts (rowid, issue_id, board_id, title, body_md, resolution)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (issue_rowid, issue_id, board_id, title, body_md, resolution or ""),
    )


def get_issue_by_ref(conn: sqlite3.Connection, board_id: str, ref_kind: str, ref_val: int | str) -> sqlite3.Row | None:
    where_col = "i.number = ?" if ref_kind == "number" else "i.id = ?"
    return conn.execute(
        f"""
        SELECT i.*, i.rowid AS _rowid,
               op.handle AS opened_by_handle,
               asg.handle AS assignee_handle
        FROM issues i
        JOIN principals op ON op.id = i.opened_by
        LEFT JOIN principals asg ON asg.id = i.assignee_id
        WHERE i.board_id = ? AND {where_col}
        """,
        (board_id, ref_val),
    ).fetchone()


def update_issue_fields(
    conn: sqlite3.Connection,
    *,
    issue_id: str,
    status: str,
    assignee_id: str | None,
    resolved_at: str | None,
    resolution: str | None,
    updated_at: str,
) -> None:
    conn.execute(
        """
        UPDATE issues
        SET status = ?, assignee_id = ?, resolved_at = ?, resolution = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, assignee_id, resolved_at, resolution, updated_at, issue_id),
    )


def list_issues(
    conn: sqlite3.Connection,
    board_id: str,
    *,
    status: str | None = None,
    severity: str | None = None,
) -> list[sqlite3.Row]:
    clauses = ["i.board_id = ?"]
    params: list[object] = [board_id]
    if status == "active":
        clauses.append("i.status IN ('open', 'in_progress')")
    elif status and status != "all":
        clauses.append("i.status = ?")
        params.append(status)
    if severity and severity != "all":
        clauses.append("i.severity = ?")
        params.append(severity)
    where_sql = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT i.*, op.handle AS opened_by_handle, asg.handle AS assignee_handle
        FROM issues i
        JOIN principals op ON op.id = i.opened_by
        LEFT JOIN principals asg ON asg.id = i.assignee_id
        WHERE {where_sql}
        ORDER BY
            CASE i.severity
                WHEN 'blocker' THEN 0
                WHEN 'high' THEN 1
                WHEN 'normal' THEN 2
                ELSE 3
            END ASC,
            i.opened_at ASC,
            i.number ASC
        """,
        params,
    ).fetchall()


def list_issue_events(conn: sqlite3.Connection, issue_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT ie.*, p.handle AS actor_handle
        FROM issue_events ie
        JOIN principals p ON p.id = ie.actor_id
        WHERE ie.issue_id = ?
        ORDER BY ie.at ASC, ie.id ASC
        """,
        (issue_id,),
    ).fetchall()


def list_retest_reports_for_board(conn: sqlite3.Connection, board_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.id, m.seq, m.created_at, m.data, m.body_md, p.handle AS author_handle
        FROM messages m
        JOIN principals p ON p.id = m.author_id
        WHERE m.board_id = ? AND m.type = 'retest_report' AND m.redacted_at IS NULL
        ORDER BY m.seq DESC
        LIMIT 100
        """,
        (board_id,),
    ).fetchall()
