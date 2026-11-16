"""SQL helpers for append-only audit log."""

from __future__ import annotations

import sqlite3


def insert_audit_log(
    conn: sqlite3.Connection,
    *,
    audit_id: str,
    at: str,
    principal_id: str | None,
    token_prefix: str | None,
    action: str,
    board_id: str | None,
    target: str | None,
    ip: str | None,
    ok: bool,
    detail_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (
            id, at, principal_id, token_prefix, action, board_id, target, ip, ok, detail_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (audit_id, at, principal_id, token_prefix, action, board_id, target, ip, int(ok), detail_json),
    )


def list_audit_logs(conn: sqlite3.Connection, *, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*, p.handle AS principal_handle, b.slug AS board_slug
        FROM audit_log a
        LEFT JOIN principals p ON p.id = a.principal_id
        LEFT JOIN boards b ON b.id = a.board_id
        ORDER BY a.at DESC, a.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
