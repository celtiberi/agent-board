"""SQL helpers for FTS5 queries across messages, decisions, and issues."""

from __future__ import annotations

import sqlite3


def search_messages_fts(
    conn: sqlite3.Connection,
    *,
    match_expr: str,
    board_ids: list[str],
    types: list[str] | None = None,
    author_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    if not board_ids:
        return []
    b_placeholders = ",".join("?" for _ in board_ids)
    clauses = ["messages_fts MATCH ?", f"m.board_id IN ({b_placeholders})", "m.redacted_at IS NULL"]
    params: list[object] = [match_expr, *board_ids]
    if types:
        t_placeholders = ",".join("?" for _ in types)
        clauses.append(f"m.type IN ({t_placeholders})")
        params.extend(types)
    if author_id:
        clauses.append("m.author_id = ?")
        params.append(author_id)
    if date_from:
        clauses.append("m.created_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("m.created_at <= ?")
        params.append(date_to)
    params.append(limit)
    where_sql = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT 'message' AS kind,
               m.id,
               m.board_id,
               b.slug AS board_slug,
               m.thread_id,
               t.title AS title,
               m.type AS msg_type,
               p.handle AS author_handle,
               m.seq,
               m.created_at,
               snippet(messages_fts, 5, '<mark>', '</mark>', ' … ', 20) AS snippet,
               bm25(messages_fts, 0.0, 0.0, 0.0, 0.0, 1.5, 1.0) AS rank_score
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.message_id
        JOIN boards b ON b.id = m.board_id
        JOIN threads t ON t.id = m.thread_id
        JOIN principals p ON p.id = m.author_id
        WHERE {where_sql}
        ORDER BY rank_score ASC
        LIMIT ?
        """,
        params,
    ).fetchall()


def search_decisions_fts(
    conn: sqlite3.Connection,
    *,
    match_expr: str,
    board_ids: list[str],
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    if not board_ids:
        return []
    b_placeholders = ",".join("?" for _ in board_ids)
    clauses = ["decisions_fts MATCH ?", f"d.board_id IN ({b_placeholders})"]
    params: list[object] = [match_expr, *board_ids]
    if date_from:
        clauses.append("d.created_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("d.created_at <= ?")
        params.append(date_to)
    params.append(limit)
    where_sql = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT 'decision' AS kind,
               d.id,
               d.board_id,
               b.slug AS board_slug,
               d.number,
               d.title,
               d.status,
               p.handle AS author_handle,
               d.created_at,
               snippet(decisions_fts, 3, '<mark>', '</mark>', ' … ', 20) AS snippet,
               (bm25(decisions_fts, 0.0, 0.0, 2.0, 1.5, 0.8)
                * CASE WHEN d.status = 'active' THEN 2.0 ELSE 0.8 END) AS rank_score
        FROM decisions_fts
        JOIN decisions d ON d.id = decisions_fts.decision_id
        JOIN boards b ON b.id = d.board_id
        JOIN principals p ON p.id = d.recorded_by
        WHERE {where_sql}
        ORDER BY rank_score ASC
        LIMIT ?
        """,
        params,
    ).fetchall()


def search_issues_fts(
    conn: sqlite3.Connection,
    *,
    match_expr: str,
    board_ids: list[str],
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    if not board_ids:
        return []
    b_placeholders = ",".join("?" for _ in board_ids)
    clauses = ["issues_fts MATCH ?", f"i.board_id IN ({b_placeholders})"]
    params: list[object] = [match_expr, *board_ids]
    if date_from:
        clauses.append("i.opened_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("i.opened_at <= ?")
        params.append(date_to)
    params.append(limit)
    where_sql = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT 'issue' AS kind,
               i.id,
               i.board_id,
               b.slug AS board_slug,
               i.number,
               i.title,
               i.status,
               i.thread_id,
               p.handle AS author_handle,
               i.opened_at AS created_at,
               snippet(issues_fts, 3, '<mark>', '</mark>', ' … ', 20) AS snippet,
               (bm25(issues_fts, 0.0, 0.0, 2.0, 1.0, 0.8)
                * CASE WHEN i.status IN ('open', 'in_progress') THEN 1.5 ELSE 0.9 END) AS rank_score
        FROM issues_fts
        JOIN issues i ON i.id = issues_fts.issue_id
        JOIN boards b ON b.id = i.board_id
        JOIN principals p ON p.id = i.opened_by
        WHERE {where_sql}
        ORDER BY rank_score ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
