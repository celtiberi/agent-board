"""SQL helpers for decisions, decision_versions, and decisions_fts."""

from __future__ import annotations

import sqlite3


def insert_decision(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    board_id: str,
    number: int,
    title: str,
    statement: str,
    rationale: str | None,
    decided_by: str,
    decided_on: str,
    recorded_by: str,
    tags_json: str,
    source_message_id: str,
    now_iso: str,
) -> int:
    conn.execute(
        """
        INSERT INTO decisions (
            id, board_id, number, title, statement, rationale,
            decided_by, decided_on, recorded_by, status, superseded_by,
            tags, source_message_id, version, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, 1, ?, ?)
        """,
        (
            decision_id,
            board_id,
            number,
            title,
            statement,
            rationale,
            decided_by,
            decided_on,
            recorded_by,
            tags_json,
            source_message_id,
            now_iso,
            now_iso,
        ),
    )
    row = conn.execute("SELECT rowid FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    return int(row["rowid"])


def insert_decision_version(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    version: int,
    snapshot_json: str,
    changed_by: str,
    changed_at: str,
    message_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO decision_versions (
            decision_id, version, snapshot_json, changed_by, changed_at, message_id
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (decision_id, version, snapshot_json, changed_by, changed_at, message_id),
    )


def upsert_decision_fts(
    conn: sqlite3.Connection,
    *,
    decision_rowid: int,
    decision_id: str,
    board_id: str,
    title: str,
    statement: str,
    rationale: str | None,
) -> None:
    conn.execute("DELETE FROM decisions_fts WHERE rowid = ?", (decision_rowid,))
    conn.execute(
        """
        INSERT INTO decisions_fts (rowid, decision_id, board_id, title, statement, rationale)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (decision_rowid, decision_id, board_id, title, statement, rationale or ""),
    )


def get_decision_by_ref(
    conn: sqlite3.Connection, board_id: str, ref_kind: str, ref_val: int | str
) -> sqlite3.Row | None:
    where_col = "d.number = ?" if ref_kind == "number" else "d.id = ?"
    return conn.execute(
        f"""
        SELECT d.*, d.rowid AS _rowid, p.handle AS recorded_by_handle,
               sup.number AS superseded_by_number
        FROM decisions d
        JOIN principals p ON p.id = d.recorded_by
        LEFT JOIN decisions sup ON sup.id = d.superseded_by
        WHERE d.board_id = ? AND {where_col}
        """,
        (board_id, ref_val),
    ).fetchone()


def mark_decision_superseded(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    superseded_by_id: str,
    new_version: int,
    updated_at: str,
) -> None:
    conn.execute(
        """
        UPDATE decisions
        SET status = 'superseded', superseded_by = ?, version = ?, updated_at = ?
        WHERE id = ?
        """,
        (superseded_by_id, new_version, updated_at, decision_id),
    )


def mark_decision_revoked(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    new_version: int,
    updated_at: str,
) -> None:
    conn.execute(
        """
        UPDATE decisions
        SET status = 'revoked', version = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_version, updated_at, decision_id),
    )


def list_decisions(conn: sqlite3.Connection, board_id: str, *, status: str | None = None) -> list[sqlite3.Row]:
    clauses = ["d.board_id = ?"]
    params: list[object] = [board_id]
    if status and status != "all":
        clauses.append("d.status = ?")
        params.append(status)
    where_sql = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT d.*, p.handle AS recorded_by_handle,
               sup.number AS superseded_by_number
        FROM decisions d
        JOIN principals p ON p.id = d.recorded_by
        LEFT JOIN decisions sup ON sup.id = d.superseded_by
        WHERE {where_sql}
        ORDER BY d.number ASC
        """,
        params,
    ).fetchall()


def list_decision_versions(conn: sqlite3.Connection, decision_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT dv.*, p.handle AS changed_by_handle
        FROM decision_versions dv
        JOIN principals p ON p.id = dv.changed_by
        WHERE dv.decision_id = ?
        ORDER BY dv.version ASC
        """,
        (decision_id,),
    ).fetchall()
