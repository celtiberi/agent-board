"""Domain service for computing live Project Briefs from canonical tables."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_board.ids import utc_now_iso
from agent_board.services.auth import require_board_role
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.delivery import format_dnd_status
from agent_board.store import boards as boards_store
from agent_board.store import decisions as decisions_store
from agent_board.store import delivery as delivery_store
from agent_board.store import issues as issues_store
from agent_board.store import messages as messages_store

_BRIEF_MAX_BYTES = 8192
_MAX_FULL_DECISIONS = 40


def _headline(text: str, limit: int = 140) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _find_open_tasks_and_questions(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    caller_id: str,
    caller_handle: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    status_rows = conn.execute(
        """
        SELECT m.id, m.seq, m.data
        FROM messages m
        WHERE m.board_id = ? AND m.type = 'status' AND m.redacted_at IS NULL
              AND m.data IS NOT NULL
        ORDER BY m.seq DESC
        LIMIT 500
        """,
        (board_id,),
    ).fetchall()

    latest_state_by_target: dict[str, str] = {}
    for row in status_rows:
        d = json.loads(row["data"])
        state = d.get("state")
        if not state:
            continue
        for key in ("task_message_id", "target_message_id"):
            tid = d.get(key)
            if tid and tid not in latest_state_by_target:
                latest_state_by_target[tid] = str(state).lower()

    task_rows = conn.execute(
        """
        SELECT m.id, m.seq, m.body_md, m.data, m.created_at, m.thread_id,
               p.handle AS author_handle, t.title AS thread_title
        FROM messages m
        JOIN principals p ON p.id = m.author_id
        JOIN threads t ON t.id = m.thread_id
        WHERE m.board_id = ? AND m.type = 'task' AND m.redacted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM messages m2 WHERE m2.supersedes_id = m.id)
        ORDER BY m.seq DESC
        LIMIT 200
        """,
        (board_id,),
    ).fetchall()

    question_rows = conn.execute(
        """
        SELECT m.id, m.seq, m.body_md, m.data, m.created_at, m.thread_id,
               p.handle AS author_handle, t.title AS thread_title
        FROM messages m
        JOIN principals p ON p.id = m.author_id
        JOIN threads t ON t.id = m.thread_id
        WHERE m.board_id = ? AND m.type = 'question' AND m.redacted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM messages m2 WHERE m2.supersedes_id = m.id)
        ORDER BY m.seq DESC
        LIMIT 200
        """,
        (board_id,),
    ).fetchall()

    to_by_msg = messages_store.get_mentions_for_messages(
        conn, [r["id"] for r in task_rows] + [r["id"] for r in question_rows]
    )

    open_tasks: list[dict[str, Any]] = []
    for row in task_rows:
        data = json.loads(row["data"]) if row["data"] else {}
        explicit_assignee = str(data.get("assignee", "")).strip().lower()
        if explicit_assignee:
            if explicit_assignee != caller_handle:
                continue
        else:
            if (
                caller_handle not in to_by_msg.get(row["id"], [])
                or str(row["author_handle"]).lower() == caller_handle
            ):
                continue

        eff_state = latest_state_by_target.get(row["id"], str(data.get("state", "open")).lower())
        if eff_state in ("done", "cancelled", "closed", "resolved"):
            continue

        if not explicit_assignee:
            completed = conn.execute(
                """
                SELECT 1
                FROM messages r
                WHERE r.board_id = ?
                  AND r.thread_id = ?
                  AND r.seq > ?
                  AND r.author_id = ?
                  AND r.redacted_at IS NULL
                  AND (
                      r.reply_to_id = ?
                      OR (r.reply_to_id IS NULL AND r.type IN ('status', 'retest_report'))
                  )
                LIMIT 1
                """,
                (board_id, row["thread_id"], int(row["seq"]), caller_id, row["id"]),
            ).fetchone()
            if completed is not None:
                continue

        open_tasks.append(
            {
                "id": row["id"],
                "seq": int(row["seq"]),
                "thread_id": row["thread_id"],
                "thread_title": row["thread_title"],
                "author_handle": row["author_handle"],
                "due": data.get("due"),
                "state": eff_state,
                "headline": _headline(row["body_md"]),
                "created_at": row["created_at"],
            }
        )
        if len(open_tasks) >= 20:
            break
    open_tasks.reverse()

    unanswered_questions: list[dict[str, Any]] = []
    for row in question_rows:
        data = json.loads(row["data"]) if row["data"] else {}
        explicit_for = str(data.get("for", "")).strip().lower()
        if explicit_for:
            if explicit_for != caller_handle:
                continue
        else:
            if (
                caller_handle not in to_by_msg.get(row["id"], [])
                or str(row["author_handle"]).lower() == caller_handle
            ):
                continue

        status_override = latest_state_by_target.get(row["id"])
        if status_override in ("answered", "cancelled", "done"):
            continue

        if not explicit_for:
            replied = conn.execute(
                """
                SELECT 1
                FROM messages r
                WHERE r.board_id = ?
                  AND r.thread_id = ?
                  AND r.seq > ?
                  AND r.redacted_at IS NULL
                  AND (r.reply_to_id IS NULL OR r.reply_to_id = ?)
                  AND (
                      r.author_id = ?
                      OR EXISTS (
                          SELECT 1 FROM memberships mb
                          WHERE mb.board_id = ? AND mb.principal_id = r.author_id AND mb.role = 'owner'
                      )
                  )
                LIMIT 1
                """,
                (board_id, row["thread_id"], int(row["seq"]), row["id"], caller_id, board_id),
            ).fetchone()
        else:
            replied = conn.execute(
                """
                SELECT 1
                FROM messages r
                WHERE r.reply_to_id = ?
                  AND r.redacted_at IS NULL
                  AND (
                      r.author_id = ?
                      OR EXISTS (
                          SELECT 1 FROM memberships mb
                          WHERE mb.board_id = ? AND mb.principal_id = r.author_id AND mb.role = 'owner'
                      )
                  )
                LIMIT 1
                """,
                (row["id"], caller_id, board_id),
            ).fetchone()
        if replied is not None:
            continue
        unanswered_questions.append(
            {
                "id": row["id"],
                "seq": int(row["seq"]),
                "thread_id": row["thread_id"],
                "thread_title": row["thread_title"],
                "author_handle": row["author_handle"],
                "headline": _headline(row["body_md"]),
                "created_at": row["created_at"],
            }
        )
        if len(unanswered_questions) >= 20:
            break
    unanswered_questions.reverse()
    return open_tasks, unanswered_questions


def _format_members_line(members: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in members:
        tags = m.get("tags") or []
        entry = f"@{m['handle']} [{', '.join(tags)}]" if tags else f"@{m['handle']}"
        if (m.get("dnd") or {}).get("active"):
            entry += " (DND)"
        parts.append(entry)
    return f"**Members:** {', '.join(parts)}"


def _render_brief_markdown(
    *,
    board: sqlite3.Row,
    members: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    titles_only: bool,
    open_issues: list[dict[str, Any]],
    latest_summary: dict[str, Any] | None,
    recent_activity: list[dict[str, Any]],
    open_tasks: list[dict[str, Any]],
    unanswered_questions: list[dict[str, Any]],
    thread_handoffs: list[dict[str, Any]],
    read_seq: int,
    unread_count: int,
    latest_seq: int,
    summarize_hint: bool,
) -> str:
    lines: list[str] = [
        f"# Project Brief: {board['name']} (`{board['slug']}`)",
        "",
        board["purpose"],
    ]
    if members:
        lines.extend(["", _format_members_line(members)])
    lines.extend(["", f"## Active Decisions ({len(decisions)})"])
    if not decisions:
        lines.append("_None yet._")
    else:
        if titles_only:
            lines.append(
                "_Showing decision titles only to keep brief compact; call `list_decisions` for full statements._"
            )
        for d in decisions:
            if titles_only:
                lines.append(f"- **{d['ref']}** ({d['decided_on']}, {d['decided_by']}): {d['title']}")
            else:
                lines.append(f"- **{d['ref']}** ({d['decided_on']}, {d['decided_by']}): {d['statement']}")

    lines.extend(["", f"## Open Issues ({len(open_issues)})"])
    if not open_issues:
        lines.append("_None open._")
    else:
        for iss in open_issues:
            assignee = iss["assignee_handle"] or "unassigned"
            opened_date = iss["opened_at"][:10]
            lines.append(
                f"- **{iss['ref']}** [{iss['severity']}] ({iss['status']}) "
                f"{iss['title']} ({assignee}, opened {opened_date})"
            )

    if thread_handoffs:
        lines.extend(["", f"## Active Thread Handoffs ({len(thread_handoffs)})"])
        for h in thread_handoffs:
            who = f"@{h['updated_by_handle']}" if h.get("updated_by_handle") else "unknown"
            summary = _headline(h["handoff_md"], limit=220)
            lines.append(f"- **[{h['thread_title']}]** ({who}): {summary}")

    lines.extend(["", "## Latest Summary"])
    if latest_summary is None:
        lines.append("_No summary posted yet._")
    else:
        lines.append(
            f"_Covers seq {latest_summary['covers_from_seq']}–{latest_summary['covers_to_seq']} "
            f"(by {latest_summary['author_handle']} at {latest_summary['created_at']}):_"
        )
        lines.append(latest_summary["body_md"].strip())
    if summarize_hint:
        lines.append("")
        lines.append(
            "> **Note:** More than 100 new messages have arrived since the last summary; "
            "consider posting a `summary` message."
        )

    lines.extend(["", f"## Recent Activity ({len(recent_activity)})"])
    if not recent_activity:
        lines.append("_No newer messages._")
    else:
        for item in recent_activity:
            lines.append(
                f"- `#{item['seq']}` [{item['type']}] **{item['author_handle']}** "
                f"in _{item['thread_title']}_: {item['headline']}"
            )

    if open_tasks or unanswered_questions:
        lines.extend(["", "## Action Items For You"])
        for t in open_tasks:
            due = f" (due {t['due']})" if t.get("due") else ""
            lines.append(f"- [task `#{t['seq']}`]{due} from {t['author_handle']}: {t['headline']}")
        for q in unanswered_questions:
            lines.append(f"- [question `#{q['seq']}`] from {q['author_handle']}: {q['headline']}")

    lines.extend(
        [
            "",
            "## Your Cursor",
            f"- `read_seq`: **{read_seq}** / `latest_seq`: **{latest_seq}** · Unread messages: **{unread_count}**",
        ]
    )
    return "\n".join(lines)


def get_brief(services: AppServices, ctx: AuthContext, *, board_slug: str) -> dict[str, Any]:
    with services.db.read_snapshot() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
        board_id = board["id"]
        latest_seq = int(board["next_seq"]) - 1
        read_seq = delivery_store.get_cursor(conn, ctx.principal_id, board_id)
        unread_count = delivery_store.count_unread_for_principal(conn, board_id, ctx.principal_id, read_seq)

        dec_rows = decisions_store.list_decisions(conn, board_id, status="active")
        active_decisions = [
            {
                "id": d["id"],
                "ref": f"D-{d['number']}",
                "title": d["title"],
                "statement": d["statement"],
                "decided_by": d["decided_by"],
                "decided_on": d["decided_on"],
                "recorded_by_handle": d["recorded_by_handle"],
            }
            for d in dec_rows
        ]

        issue_rows = issues_store.list_issues(conn, board_id, status="active")
        open_issues = [
            {
                "id": i["id"],
                "ref": f"I-{i['number']}",
                "title": i["title"],
                "severity": i["severity"],
                "status": i["status"],
                "assignee_handle": i["assignee_handle"],
                "opened_at": i["opened_at"],
            }
            for i in issue_rows
        ]

        summary_row = conn.execute(
            """
            SELECT m.id, m.seq, m.body_md, m.data, m.created_at, p.handle AS author_handle
            FROM messages m
            JOIN principals p ON p.id = m.author_id
            WHERE m.board_id = ? AND m.type = 'summary' AND m.redacted_at IS NULL
            ORDER BY m.seq DESC
            LIMIT 1
            """,
            (board_id,),
        ).fetchone()

        latest_summary: dict[str, Any] | None = None
        after_seq = 0
        if summary_row is not None:
            s_data = json.loads(summary_row["data"]) if summary_row["data"] else {}
            covers_to = int(s_data.get("covers_to_seq", summary_row["seq"]))
            covers_from = int(s_data.get("covers_from_seq", 1))
            after_seq = covers_to
            latest_summary = {
                "id": summary_row["id"],
                "seq": int(summary_row["seq"]),
                "covers_from_seq": covers_from,
                "covers_to_seq": covers_to,
                "author_handle": summary_row["author_handle"],
                "body_md": summary_row["body_md"],
                "created_at": summary_row["created_at"],
            }

        summarize_hint = (latest_seq - after_seq) > 100

        recent_rows = conn.execute(
            """
            SELECT m.id, m.seq, m.type, m.body_md, m.created_at, m.thread_id,
                   p.handle AS author_handle, t.title AS thread_title
            FROM messages m
            JOIN principals p ON p.id = m.author_id
            JOIN threads t ON t.id = m.thread_id
            WHERE m.board_id = ?
              AND m.seq > ?
              AND NOT EXISTS (SELECT 1 FROM messages m2 WHERE m2.supersedes_id = m.id)
            ORDER BY m.seq DESC
            LIMIT 30
            """,
            (board_id, after_seq),
        ).fetchall()
        recent_activity = [
            {
                "id": r["id"],
                "seq": int(r["seq"]),
                "type": r["type"],
                "author_handle": r["author_handle"],
                "thread_id": r["thread_id"],
                "thread_title": r["thread_title"],
                "headline": _headline(r["body_md"]),
                "created_at": r["created_at"],
            }
            for r in reversed(recent_rows)
        ]

        open_tasks, unanswered_questions = _find_open_tasks_and_questions(
            conn,
            board_id=board_id,
            caller_id=ctx.principal_id,
            caller_handle=ctx.handle,
        )
        handoff_rows = boards_store.list_thread_handoffs(conn, board_id)
        thread_handoffs = [
            {
                "thread_id": h["thread_id"],
                "thread_title": h["thread_title"],
                "handoff_md": h["handoff_md"],
                "updated_by_handle": h["updated_by_handle"],
                "updated_at": h["handoff_updated_at"],
            }
            for h in handoff_rows
        ]
        member_rows = boards_store.list_memberships_for_board(conn, board_id)
        now_iso = utc_now_iso()
        members = [
            {
                "handle": m["handle"],
                "role": m["role"],
                "tags": json.loads(m["tags"] if "tags" in m.keys() and m["tags"] else "[]"),
                "dnd": format_dnd_status(m, now_iso=now_iso),
            }
            for m in member_rows
            if m["kind"] != "system" and m["disabled_at"] is None
        ]

    titles_only = len(active_decisions) > _MAX_FULL_DECISIONS
    markdown = _render_brief_markdown(
        board=board,
        members=members,
        decisions=active_decisions,
        titles_only=titles_only,
        open_issues=open_issues,
        latest_summary=latest_summary,
        recent_activity=recent_activity,
        open_tasks=open_tasks,
        unanswered_questions=unanswered_questions,
        thread_handoffs=thread_handoffs,
        read_seq=read_seq,
        unread_count=unread_count,
        latest_seq=latest_seq,
        summarize_hint=summarize_hint,
    )
    if not titles_only and len(markdown.encode("utf-8")) > _BRIEF_MAX_BYTES and active_decisions:
        titles_only = True
        markdown = _render_brief_markdown(
            board=board,
            members=members,
            decisions=active_decisions,
            titles_only=True,
            open_issues=open_issues,
            latest_summary=latest_summary,
            recent_activity=recent_activity,
            open_tasks=open_tasks,
            unanswered_questions=unanswered_questions,
            thread_handoffs=thread_handoffs,
            read_seq=read_seq,
            unread_count=unread_count,
            latest_seq=latest_seq,
            summarize_hint=summarize_hint,
        )
    if len(markdown.encode("utf-8")) > _BRIEF_MAX_BYTES and len(thread_handoffs) > 5:
        markdown = _render_brief_markdown(
            board=board,
            members=members,
            decisions=active_decisions,
            titles_only=titles_only,
            open_issues=open_issues,
            latest_summary=latest_summary,
            recent_activity=recent_activity,
            open_tasks=open_tasks,
            unanswered_questions=unanswered_questions,
            thread_handoffs=thread_handoffs[:5],
            read_seq=read_seq,
            unread_count=unread_count,
            latest_seq=latest_seq,
            summarize_hint=summarize_hint,
        )

    return {
        "board": {
            "id": board["id"],
            "slug": board["slug"],
            "name": board["name"],
            "purpose": board["purpose"],
        },
        "members": members,
        "active_decisions": active_decisions,
        "decisions_titles_only": titles_only,
        "open_issues": open_issues,
        "thread_handoffs": thread_handoffs,
        "latest_summary": latest_summary,
        "recent_activity": recent_activity,
        "open_tasks_for_caller": open_tasks,
        "unanswered_questions_for_caller": unanswered_questions,
        "cursor": {
            "read_seq": read_seq,
            "unread_count": unread_count,
            "latest_seq": latest_seq,
        },
        "summarize_hint": summarize_hint,
        "markdown": markdown,
    }
