"""SQL helpers for messages, mentions, attachments, and message FTS synchronization."""

from __future__ import annotations

import sqlite3


def insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    board_id: str,
    thread_id: str,
    seq: int,
    author_id: str,
    msg_type: str,
    body_md: str,
    data_json: str | None,
    warnings_json: str,
    reply_to_id: str | None,
    supersedes_id: str | None,
    root_id: str,
    root_seq: int,
    created_at: str,
) -> int:
    conn.execute(
        """
        INSERT INTO messages (
            id, board_id, thread_id, seq, author_id, type, body_md, data,
            warnings_json, reply_to_id, supersedes_id, root_id, root_seq, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            board_id,
            thread_id,
            seq,
            author_id,
            msg_type,
            body_md,
            data_json,
            warnings_json,
            reply_to_id,
            supersedes_id,
            root_id,
            root_seq,
            created_at,
        ),
    )
    row = conn.execute("SELECT rowid FROM messages WHERE id = ?", (root_id,)).fetchone()
    return int(row["rowid"])


def upsert_message_fts(
    conn: sqlite3.Connection,
    *,
    root_rowid: int,
    message_id: str,
    root_id: str,
    board_id: str,
    thread_id: str,
    thread_title: str,
    body_md: str,
) -> None:
    conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (root_rowid,))
    conn.execute(
        """
        INSERT INTO messages_fts (
            rowid, message_id, root_id, board_id, thread_id, thread_title, body_md
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (root_rowid, message_id, root_id, board_id, thread_id, thread_title, body_md),
    )


def delete_message_fts_by_root(conn: sqlite3.Connection, root_id: str) -> None:
    row = conn.execute("SELECT rowid FROM messages WHERE id = ?", (root_id,)).fetchone()
    if row is not None:
        conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (int(row["rowid"]),))


def insert_mentions(conn: sqlite3.Connection, message_id: str, principal_ids: list[str]) -> None:
    if not principal_ids:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO mentions (message_id, principal_id) VALUES (?, ?)",
        [(message_id, pid) for pid in principal_ids],
    )


def get_message_by_id(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT m.*, p.handle AS author_handle, b.slug AS board_slug, t.title AS thread_title
        FROM messages m
        JOIN principals p ON p.id = m.author_id
        JOIN boards b ON b.id = m.board_id
        JOIN threads t ON t.id = m.thread_id
        WHERE m.id = ?
        """,
        (message_id,),
    ).fetchone()


def get_latest_message_for_root(conn: sqlite3.Connection, root_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT m.*, p.handle AS author_handle, b.slug AS board_slug, t.title AS thread_title
        FROM messages m
        JOIN principals p ON p.id = m.author_id
        JOIN boards b ON b.id = m.board_id
        JOIN threads t ON t.id = m.thread_id
        WHERE m.root_id = ?
        ORDER BY m.seq DESC
        LIMIT 1
        """,
        (root_id,),
    ).fetchone()


def get_message_versions_by_root(conn: sqlite3.Connection, root_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.*, p.handle AS author_handle, b.slug AS board_slug, t.title AS thread_title
        FROM messages m
        JOIN principals p ON p.id = m.author_id
        JOIN boards b ON b.id = m.board_id
        JOIN threads t ON t.id = m.thread_id
        WHERE m.root_id = ?
        ORDER BY m.seq ASC
        """,
        (root_id,),
    ).fetchall()


def redact_message_chain(
    conn: sqlite3.Connection,
    *,
    root_id: str,
    redacted_body: str,
    redacted_at: str,
) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT id, seq FROM messages WHERE root_id = ?", (root_id,)).fetchall()
    conn.execute(
        """
        UPDATE messages
        SET body_md = ?, data = NULL, warnings_json = '[]', redacted_at = ?
        WHERE root_id = ?
        """,
        (redacted_body, redacted_at, root_id),
    )
    return rows


def read_messages_stream(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    since_seq: int,
    thread_id: str | None = None,
    types: list[str] | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    clauses = ["m.board_id = ?", "m.seq > ?"]
    params: list[object] = [board_id, since_seq]
    if thread_id:
        clauses.append("m.thread_id = ?")
        params.append(thread_id)
    if types:
        placeholders = ",".join("?" for _ in types)
        clauses.append(f"m.type IN ({placeholders})")
        params.extend(types)
    params.append(limit)
    where_sql = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT m.*, p.handle AS author_handle, b.slug AS board_slug, t.title AS thread_title
        FROM messages m
        JOIN principals p ON p.id = m.author_id
        JOIN boards b ON b.id = m.board_id
        JOIN threads t ON t.id = m.thread_id
        WHERE {where_sql}
        ORDER BY m.seq ASC
        LIMIT ?
        """,
        params,
    ).fetchall()


def read_thread_snapshot(conn: sqlite3.Connection, thread_id: str) -> list[sqlite3.Row]:
    """Return latest version per root_id ordered by root_seq ASC."""
    return conn.execute(
        """
        WITH ranked AS (
            SELECT m.*, p.handle AS author_handle, b.slug AS board_slug, t.title AS thread_title,
                   ROW_NUMBER() OVER (PARTITION BY m.root_id ORDER BY m.seq DESC) AS rn,
                   COUNT(*) OVER (PARTITION BY m.root_id) AS version_count,
                   MAX(m.created_at) OVER (PARTITION BY m.root_id) AS latest_created_at
            FROM messages m
            JOIN principals p ON p.id = m.author_id
            JOIN boards b ON b.id = m.board_id
            JOIN threads t ON t.id = m.thread_id
            WHERE m.thread_id = ?
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        ORDER BY root_seq ASC
        """,
        (thread_id,),
    ).fetchall()


def get_mentions_for_messages(conn: sqlite3.Connection, message_ids: list[str]) -> dict[str, list[str]]:
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"""
        SELECT mn.message_id, p.handle
        FROM mentions mn
        JOIN principals p ON p.id = mn.principal_id
        WHERE mn.message_id IN ({placeholders})
        ORDER BY p.handle
        """,
        message_ids,
    ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row["message_id"], []).append(row["handle"])
    return result


def get_attachments_for_messages(conn: sqlite3.Connection, message_ids: list[str]) -> dict[str, list[sqlite3.Row]]:
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM attachments
        WHERE message_id IN ({placeholders})
        ORDER BY created_at ASC
        """,
        message_ids,
    ).fetchall()
    result: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        result.setdefault(row["message_id"], []).append(row)
    return result


def insert_attachment(
    conn: sqlite3.Connection,
    *,
    attachment_id: str,
    board_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    sha256: str,
    uploaded_by: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO attachments (
            id, board_id, message_id, filename, content_type, size_bytes, sha256, uploaded_by, created_at
        )
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            attachment_id,
            board_id,
            filename,
            content_type,
            size_bytes,
            sha256,
            uploaded_by,
            created_at,
        ),
    )


def get_attachment_by_id(conn: sqlite3.Connection, attachment_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()


def get_board_attachments_size(conn: sqlite3.Connection, board_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM attachments WHERE board_id = ?",
        (board_id,),
    ).fetchone()
    return int(row["total"]) if row else 0


def link_attachments_to_message(
    conn: sqlite3.Connection,
    *,
    attachment_ids: list[str],
    board_id: str,
    message_id: str,
) -> int:
    if not attachment_ids:
        return 0
    placeholders = ",".join("?" for _ in attachment_ids)
    cur = conn.execute(
        f"""
        UPDATE attachments
        SET message_id = ?
        WHERE id IN ({placeholders}) AND board_id = ? AND message_id IS NULL
        """,
        [message_id, *attachment_ids, board_id],
    )
    return cur.rowcount


def list_orphan_attachments_before(conn: sqlite3.Connection, cutoff_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM attachments WHERE message_id IS NULL AND created_at < ?",
        (cutoff_iso,),
    ).fetchall()


def delete_attachment_by_id(conn: sqlite3.Connection, attachment_id: str) -> None:
    conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))


def count_sha256_references(conn: sqlite3.Connection, sha256: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS cnt FROM attachments WHERE sha256 = ?", (sha256,)).fetchone()
    return int(row["cnt"]) if row else 0
