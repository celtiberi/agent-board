"""SQL helpers for backup run history."""

from __future__ import annotations

import sqlite3


def insert_backup_run(
    conn: sqlite3.Connection,
    *,
    backup_id: str,
    started_at: str,
    finished_at: str,
    ok: bool,
    db_backup_path: str | None,
    size_bytes: int,
    detail: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO backup_runs (
            id, started_at, finished_at, ok, db_backup_path, size_bytes, detail
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (backup_id, started_at, finished_at, int(ok), db_backup_path, size_bytes, detail),
    )


def get_latest_successful_backup(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM backup_runs WHERE ok = 1 ORDER BY finished_at DESC LIMIT 1").fetchone()


def list_backup_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM backup_runs ORDER BY finished_at DESC LIMIT ?", (limit,)).fetchall()
