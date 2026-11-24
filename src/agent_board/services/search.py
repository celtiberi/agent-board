"""Domain service for FTS5 search across accessible boards."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from agent_board.errors import ValidationError
from agent_board.services.auth import require_board_role
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.messages import VALID_MESSAGE_TYPES
from agent_board.store import boards as boards_store
from agent_board.store import principals as principals_store
from agent_board.store import search as search_store

_QUERY_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')
_SIMPLE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_]+\*$")
_SIMPLE_WORD_RE = re.compile(r"^[A-Za-z0-9_]+$")
_FTS5_RESERVED = frozenset({"AND", "OR", "NOT", "NEAR"})


def build_fts_match_expr(raw_query: str) -> str:
    """Parse user query supporting phrases, OR, and prefix* without FTS5 syntax errors."""
    tokens: list[str] = []
    for match in _QUERY_TOKEN_RE.finditer(raw_query.strip()):
        phrase, word = match.group(1), match.group(2)
        if phrase is not None:
            clean = phrase.replace('"', '""').strip()
            if clean:
                tokens.append(f'"{clean}"')
        elif word:
            if word == "OR":
                tokens.append("OR")
            elif (
                (_SIMPLE_PREFIX_RE.match(word) or _SIMPLE_WORD_RE.match(word))
                and word.rstrip("*").upper() not in _FTS5_RESERVED
            ):
                tokens.append(word)
            else:
                has_star = word.endswith("*") and len(word) > 1
                stem = word[:-1] if has_star else word
                escaped = stem.replace('"', '""')
                if escaped:
                    tokens.append(f'"{escaped}"*' if has_star else f'"{escaped}"')

    # Clean leading/trailing/consecutive ORs
    cleaned: list[str] = []
    for tok in tokens:
        if tok == "OR":
            if not cleaned or cleaned[-1] == "OR":
                continue
            cleaned.append("OR")
        else:
            cleaned.append(tok)
    if cleaned and cleaned[-1] == "OR":
        cleaned.pop()

    if not cleaned:
        raise ValidationError("Search query must contain at least one search term")
    return " ".join(cleaned)


def search(
    services: AppServices,
    ctx: AuthContext,
    *,
    query: str,
    board_slug: str | None = None,
    types: list[str] | None = None,
    author_handle: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if not query or not query.strip():
        raise ValidationError("Search query 'q' is required")
    if limit < 1 or limit > 200:
        raise ValidationError("limit must be between 1 and 200")
    if types:
        bad = [t for t in types if t not in VALID_MESSAGE_TYPES]
        if bad:
            raise ValidationError(f"Invalid message types: {', '.join(bad)}")

    match_expr = build_fts_match_expr(query)
    conn = services.db.read_conn()

    board_ids: list[str] = []
    if board_slug:
        board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
        board_ids = [board["id"]]
    else:
        for b in boards_store.list_boards_for_principal(
            conn,
            ctx.principal_id,
            include_archived=True,
            is_server_owner=ctx.is_server_owner,
        ):
            if ctx.allows_board(b["slug"]) and ctx.effective_role(b["membership_role"]) is not None:
                board_ids.append(b["id"])

    if not board_ids:
        return {"query": query, "total": 0, "hits": []}

    author_id: str | None = None
    if author_handle:
        author_row = principals_store.get_principal_by_handle(conn, author_handle.strip().lower())
        if author_row is None:
            return {"query": query, "total": 0, "hits": []}
        author_id = author_row["id"]

    hits: list[dict[str, Any]] = []

    try:
        msg_rows = search_store.search_messages_fts(
            conn,
            match_expr=match_expr,
            board_ids=board_ids,
            types=types,
            author_id=author_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
    except sqlite3.OperationalError as exc:
        raise ValidationError(f"Invalid search query syntax: {exc}") from exc
    for r in msg_rows:
        hits.append(
            {
                "kind": "message",
                "board_slug": r["board_slug"],
                "id": r["id"],
                "ref": f"#{r['seq']}",
                "thread_id": r["thread_id"],
                "type": r["msg_type"],
                "author_handle": r["author_handle"],
                "title": r["title"],
                "snippet": r["snippet"],
                "url": f"/b/{r['board_slug']}/t/{r['thread_id']}#m-{r['id']}",
                "score": round(-float(r["rank_score"]), 4),
                "created_at": r["created_at"],
            }
        )

    include_decisions = (not types or "decision" in types) and author_id is None
    if include_decisions:
        try:
            dec_rows = search_store.search_decisions_fts(
                conn,
                match_expr=match_expr,
                board_ids=board_ids,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
        except sqlite3.OperationalError as exc:
            raise ValidationError(f"Invalid search query syntax: {exc}") from exc
        for r in dec_rows:
            ref = f"D-{r['number']}"
            hits.append(
                {
                    "kind": "decision",
                    "board_slug": r["board_slug"],
                    "id": r["id"],
                    "ref": ref,
                    "thread_id": None,
                    "type": "decision",
                    "author_handle": r["author_handle"],
                    "title": f"{ref}: {r['title']} [{r['status']}]",
                    "snippet": r["snippet"],
                    "url": f"/b/{r['board_slug']}/decisions#{ref}",
                    "score": round(-float(r["rank_score"]), 4),
                    "created_at": r["created_at"],
                }
            )

    include_issues = (not types or "issue" in types) and author_id is None
    if include_issues:
        try:
            iss_rows = search_store.search_issues_fts(
                conn,
                match_expr=match_expr,
                board_ids=board_ids,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
        except sqlite3.OperationalError as exc:
            raise ValidationError(f"Invalid search query syntax: {exc}") from exc
        for r in iss_rows:
            ref = f"I-{r['number']}"
            hits.append(
                {
                    "kind": "issue",
                    "board_slug": r["board_slug"],
                    "id": r["id"],
                    "ref": ref,
                    "thread_id": r["thread_id"],
                    "type": "issue",
                    "author_handle": r["author_handle"],
                    "title": f"{ref}: {r['title']} [{r['status']}]",
                    "snippet": r["snippet"],
                    "url": f"/b/{r['board_slug']}/issues/{ref}",
                    "score": round(-float(r["rank_score"]), 4),
                    "created_at": r["created_at"],
                }
            )

    hits.sort(key=lambda h: (h["score"], h["created_at"]), reverse=True)
    trimmed = hits[:limit]
    return {
        "query": query,
        "total": len(trimmed),
        "hits": trimmed,
    }
