"""SQL helpers for principals, API tokens, human auth, and web sessions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def get_principal_by_id(conn: sqlite3.Connection, principal_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM principals WHERE id = ?", (principal_id,)).fetchone()


def get_principal_by_handle(conn: sqlite3.Connection, handle: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM principals WHERE handle = ?", (handle,)).fetchone()


def get_first_server_owner(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM principals
        WHERE is_server_owner = 1 AND disabled_at IS NULL
        ORDER BY created_at ASC
        LIMIT 1
        """
    ).fetchone()


def list_principals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM principals ORDER BY kind, handle").fetchall()


def get_principals_by_handles(conn: sqlite3.Connection, handles: list[str]) -> list[sqlite3.Row]:
    if not handles:
        return []
    placeholders = ",".join("?" for _ in handles)
    return conn.execute(
        f"SELECT * FROM principals WHERE handle IN ({placeholders}) AND disabled_at IS NULL",
        handles,
    ).fetchall()


def insert_principal(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    handle: str,
    kind: str,
    display_name: str,
    is_server_owner: bool,
    created_at: str,
    tags_json: str = "[]",
) -> None:
    conn.execute(
        """
        INSERT INTO principals (id, handle, kind, display_name, is_server_owner, created_at, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (principal_id, handle, kind, display_name, int(is_server_owner), created_at, tags_json),
    )


def update_principal_tags(conn: sqlite3.Connection, principal_id: str, tags_json: str) -> None:
    conn.execute(
        "UPDATE principals SET tags = ? WHERE id = ?",
        (tags_json, principal_id),
    )


def update_principal_identity(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    handle: str,
    display_name: str,
) -> None:
    conn.execute(
        "UPDATE principals SET handle = ?, display_name = ? WHERE id = ?",
        (handle, display_name, principal_id),
    )


def update_principal_disabled(conn: sqlite3.Connection, principal_id: str, disabled_at: str | None) -> None:
    conn.execute(
        "UPDATE principals SET disabled_at = ? WHERE id = ?",
        (disabled_at, principal_id),
    )


def upsert_human_auth(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    password_hash: str,
    totp_secret_enc: str | None,
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO human_auth (principal_id, password_hash, totp_secret_enc, failed_attempts, locked_until, updated_at)
        VALUES (?, ?, ?, 0, NULL, ?)
        ON CONFLICT(principal_id) DO UPDATE SET
            password_hash = excluded.password_hash,
            totp_secret_enc = COALESCE(excluded.totp_secret_enc, human_auth.totp_secret_enc),
            failed_attempts = 0,
            locked_until = NULL,
            updated_at = excluded.updated_at
        """,
        (principal_id, password_hash, totp_secret_enc, updated_at),
    )


def get_human_auth(conn: sqlite3.Connection, principal_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM human_auth WHERE principal_id = ?", (principal_id,)).fetchone()


def update_human_auth_failure(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    failed_attempts: int,
    locked_until: str | None,
    updated_at: str,
) -> None:
    conn.execute(
        """
        UPDATE human_auth
        SET failed_attempts = ?, locked_until = ?, updated_at = ?
        WHERE principal_id = ?
        """,
        (failed_attempts, locked_until, updated_at, principal_id),
    )


def reset_human_auth_failures(conn: sqlite3.Connection, principal_id: str, updated_at: str) -> None:
    conn.execute(
        """
        UPDATE human_auth
        SET failed_attempts = 0, locked_until = NULL, updated_at = ?
        WHERE principal_id = ?
        """,
        (updated_at, principal_id),
    )


def insert_token(
    conn: sqlite3.Connection,
    *,
    token_id: str,
    principal_id: str,
    name: str,
    token_hash: str,
    prefix: str,
    board_scope_json: str,
    max_role: str,
    created_at: str,
    expires_at: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO tokens (
            id, principal_id, name, token_hash, prefix, board_scope,
            max_role, created_at, expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token_id,
            principal_id,
            name,
            token_hash,
            prefix,
            board_scope_json,
            max_role,
            created_at,
            expires_at,
        ),
    )


def get_token_by_hash(conn: sqlite3.Connection, token_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT t.*, p.handle AS principal_handle, p.kind AS principal_kind,
               p.display_name AS principal_display_name, p.is_server_owner,
               p.disabled_at AS principal_disabled_at
        FROM tokens t
        JOIN principals p ON p.id = t.principal_id
        WHERE t.token_hash = ?
        """,
        (token_hash,),
    ).fetchone()


def list_tokens(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.id, t.principal_id, p.handle AS principal_handle, t.name, t.prefix,
               t.board_scope, t.max_role, t.created_at, t.expires_at, t.last_used_at, t.revoked_at
        FROM tokens t
        JOIN principals p ON p.id = t.principal_id
        ORDER BY t.created_at DESC
        """
    ).fetchall()


def touch_token_last_used(conn: sqlite3.Connection, token_id: str, now_iso: str) -> None:
    conn.execute("UPDATE tokens SET last_used_at = ? WHERE id = ?", (now_iso, token_id))


def revoke_token(conn: sqlite3.Connection, token_id: str, revoked_at: str) -> int:
    cur = conn.execute(
        "UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (revoked_at, token_id),
    )
    return cur.rowcount


def sync_agent_token_board_scopes(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    add_slug: str | None = None,
    remove_slug: str | None = None,
) -> int:
    """Keep explicit-board agent token scopes aligned with board memberships."""
    rows = conn.execute(
        "SELECT id, board_scope FROM tokens WHERE principal_id = ? AND revoked_at IS NULL",
        (principal_id,),
    ).fetchall()
    updated = 0
    for row in rows:
        scope = [str(s) for s in json.loads(row["board_scope"])]
        if "*" in scope:
            continue
        new_scope = list(scope)
        if add_slug and add_slug not in new_scope:
            new_scope.append(add_slug)
        if remove_slug and remove_slug in new_scope:
            new_scope = [s for s in new_scope if s != remove_slug]
        if new_scope != scope:
            conn.execute(
                "UPDATE tokens SET board_scope = ? WHERE id = ?",
                (json.dumps(new_scope), row["id"]),
            )
            updated += 1
    return updated


def insert_web_session(
    conn: sqlite3.Connection,
    *,
    id_hash: str,
    principal_id: str,
    csrf_token: str,
    created_at: str,
    expires_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO web_sessions (id_hash, principal_id, csrf_token, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (id_hash, principal_id, csrf_token, created_at, expires_at),
    )


def get_web_session(conn: sqlite3.Connection, id_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT ws.*, p.handle AS principal_handle, p.kind AS principal_kind,
               p.display_name AS principal_display_name, p.is_server_owner,
               p.disabled_at AS principal_disabled_at
        FROM web_sessions ws
        JOIN principals p ON p.id = ws.principal_id
        WHERE ws.id_hash = ?
        """,
        (id_hash,),
    ).fetchone()


def revoke_web_session(conn: sqlite3.Connection, id_hash: str, revoked_at: str) -> None:
    conn.execute(
        "UPDATE web_sessions SET revoked_at = ? WHERE id_hash = ?",
        (revoked_at, id_hash),
    )


def update_human_totp(conn: sqlite3.Connection, principal_id: str, totp_secret_enc: str | None, updated_at: str) -> Any:
    return conn.execute(
        "UPDATE human_auth SET totp_secret_enc = ?, updated_at = ? WHERE principal_id = ?",
        (totp_secret_enc, updated_at, principal_id),
    )
