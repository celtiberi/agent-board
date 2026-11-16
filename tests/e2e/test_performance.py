"""Load & latency benchmark on 50,000 messages across 5 boards (§20 Ops target)."""

from __future__ import annotations

import json
import time

from agent_board.ids import new_ulid, utc_now_iso
from agent_board.services import boards as boards_svc
from agent_board.services import brief as brief_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services import search as search_svc
from agent_board.services.auth import set_membership
from tests.conftest import BoardTestEnv


def _p95_ms(samples_ms: list[float]) -> float:
    ordered = sorted(samples_ms)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return ordered[idx]


def test_50k_messages_latency_targets(env: BoardTestEnv) -> None:
    board_slugs = ["alpha", "perf-b2", "perf-b3", "perf-b4", "perf-b5"]
    for slug in board_slugs[1:]:
        boards_svc.create_board(env.services, env.owner_ctx, slug=slug, name=f"Board {slug}", purpose=f"Perf {slug}")
        set_membership(env.services, env.owner_ctx, board_slug=slug, principal_handle="bob", role="agent")

    # Seed realistic decisions & issues on alpha
    for idx in range(10):
        decisions_svc.record_decision(
            env.services,
            env.bob_ctx,
            board_slug="alpha",
            title=f"Perf policy {idx}",
            statement=f"Canonical decision rule #{idx} for property pre-screen.",
            decided_by="owner",
            decided_on="2026-11-16",
        )
    issues_svc.open_issue(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title="Duplicate webhook ingest",
        body_md="Need ingest dedupe for repeated event records.",
        severity="high",
        assignee="mac-dev-ai",
    )

    # Bulk insert 50,000 messages (10,000 per board) + FTS entries in one fast transaction
    now_iso = utc_now_iso()
    with env.services.db.write_tx() as conn:
        for b_idx, slug in enumerate(board_slugs):
            board = conn.execute("SELECT id, next_seq FROM boards WHERE slug = ?", (slug,)).fetchone()
            board_id = board["id"]
            start_seq = int(board["next_seq"])
            thread_id = conn.execute(
                "SELECT id FROM threads WHERE board_id = ? AND title = 'general'", (board_id,)
            ).fetchone()["id"]

            msg_rows = []
            for i in range(10_000):
                seq = start_seq + i
                mid = new_ulid()
                msg_type = "note" if i % 50 != 0 else "task"
                data_json = (
                    json.dumps({"assignee": "bob", "state": "open"}) if msg_type == "task" and i >= 9950 else None
                )
                keyword = "ingestmatchkeyword" if i % 250 == 0 else "routine"
                body = f"Perf message {b_idx}-{i} {keyword} coordination payload."
                msg_rows.append(
                    (
                        mid,
                        board_id,
                        thread_id,
                        seq,
                        env.dev_ctx.principal_id,
                        msg_type,
                        body,
                        data_json,
                        "[]",
                        None,
                        None,
                        mid,
                        seq,
                        now_iso,
                    )
                )

            conn.executemany(
                """
                INSERT INTO messages (
                    id, board_id, thread_id, seq, author_id, type, body_md, data,
                    warnings_json, reply_to_id, supersedes_id, root_id, root_seq, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                msg_rows,
            )
            conn.execute(
                "UPDATE boards SET next_seq = ? WHERE id = ?",
                (start_seq + 10_000, board_id),
            )
            # Sync FTS for newly inserted batch
            conn.execute(
                """
                INSERT INTO messages_fts (rowid, message_id, root_id, board_id, thread_id, thread_title, body_md)
                SELECT rowid, id, root_id, board_id, thread_id, 'general', body_md
                FROM messages
                WHERE board_id = ? AND seq >= ?
                """,
                (board_id, start_seq),
            )

    # Warm reader connection
    brief_svc.get_brief(env.services, env.bob_ctx, board_slug="alpha")

    brief_ms: list[float] = []
    read_ms: list[float] = []
    search_ms: list[float] = []

    for _ in range(20):
        t0 = time.perf_counter()
        brief_svc.get_brief(env.services, env.bob_ctx, board_slug="alpha")
        brief_ms.append((time.perf_counter() - t0) * 1000.0)

        t1 = time.perf_counter()
        res = messages_svc.read_messages(env.services, env.bob_ctx, board_slug="alpha", since_seq=5000, limit=200)
        assert len(res["messages"]) == 200
        read_ms.append((time.perf_counter() - t1) * 1000.0)

        t2 = time.perf_counter()
        sres = search_svc.search(env.services, env.bob_ctx, query="ingestmatchkeyword")
        assert sres["total"] > 0
        search_ms.append((time.perf_counter() - t2) * 1000.0)

    assert _p95_ms(brief_ms) < 150.0, f"Brief p95 too slow: {_p95_ms(brief_ms):.2f}ms"
    assert _p95_ms(read_ms) < 100.0, f"Read p95 too slow: {_p95_ms(read_ms):.2f}ms"
    assert _p95_ms(search_ms) < 200.0, f"Search p95 too slow: {_p95_ms(search_ms):.2f}ms"
