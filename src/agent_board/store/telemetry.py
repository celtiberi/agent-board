"""SQL helpers for workstation token/cache telemetry snapshots."""

from __future__ import annotations

import sqlite3


def upsert_telemetry_snapshot(
    conn: sqlite3.Connection,
    *,
    host_id: str,
    reported_at: str,
    payload_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO telemetry_snapshots (host_id, reported_at, payload_json)
        VALUES (?, ?, ?)
        ON CONFLICT(host_id) DO UPDATE SET
            reported_at = excluded.reported_at,
            payload_json = excluded.payload_json
        """,
        (host_id, reported_at, payload_json),
    )


def list_telemetry_snapshots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT host_id, reported_at, payload_json FROM telemetry_snapshots ORDER BY reported_at DESC"
    ).fetchall()
