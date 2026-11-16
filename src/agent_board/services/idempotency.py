"""Transactional idempotency key helper across all write operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Any

from agent_board.errors import ConflictError
from agent_board.ids import utc_now_iso
from agent_board.services.context import AuthContext
from agent_board.store import idempotency as idemp_store


def compute_request_hash(operation: str, payload: Any) -> str:
    canonical = json.dumps(
        {"op": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def execute_idempotent(
    conn: sqlite3.Connection,
    *,
    ctx: AuthContext,
    key: str | None,
    operation: str,
    request_payload: Any,
    status_code: int,
    fn: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], int, bool]:
    """Execute fn inside the current write_tx, replaying or rejecting conflicting keys."""
    if not key:
        return fn(), status_code, False

    clean_key = key.strip()
    if not clean_key:
        return fn(), status_code, False
    if len(clean_key) > 200:
        clean_key = clean_key[:200]

    req_hash = compute_request_hash(operation, request_payload)
    existing = idemp_store.get_idempotency_key(conn, ctx.principal_id, clean_key)
    if existing is not None:
        if existing["operation"] != operation or existing["request_hash"] != req_hash:
            raise ConflictError(
                "Idempotency key was already used with a different operation or payload",
                code="idempotency_conflict",
            )
        return json.loads(existing["response_json"]), int(existing["status_code"]), True

    result = fn()
    idemp_store.insert_idempotency_key(
        conn,
        principal_id=ctx.principal_id,
        key=clean_key,
        operation=operation,
        request_hash=req_hash,
        status_code=status_code,
        response_json=json.dumps(result, separators=(",", ":")),
        created_at=utc_now_iso(),
    )
    return result, status_code, False
