"""SQL helpers for idempotency keys."""

from __future__ import annotations

import sqlite3


def get_idempotency_key(conn: sqlite3.Connection, principal_id: str, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM idempotency_keys WHERE principal_id = ? AND key = ?",
        (principal_id, key),
    ).fetchone()


def insert_idempotency_key(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    key: str,
    operation: str,
    request_hash: str,
    status_code: int,
    response_json: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO idempotency_keys (
            principal_id, key, operation, request_hash, status_code, response_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (principal_id, key, operation, request_hash, status_code, response_json, created_at),
    )


def prune_idempotency_keys_before(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    cur = conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?", (cutoff_iso,))
    return cur.rowcount
