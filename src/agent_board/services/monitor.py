"""Domain service for live cross-board observability (/monitor & /api/v1/admin/monitor)."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agent_board.errors import ValidationError
from agent_board.ids import dt_to_iso, iso_to_dt, utc_now
from agent_board.services.auth import require_server_owner
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.delivery import format_dnd_status
from agent_board.store import delivery as delivery_store
from agent_board.store import messages as messages_store
from agent_board.store import telemetry as telemetry_store
from agent_board.web.rendering import format_chicago_time

_CHICAGO = ZoneInfo("America/Chicago")
_URL_RE = re.compile(r"https?://\S+")
_HEALTH_ORDER = {
    "disabled": 0,
    "failing": 1,
    "no_webhook": 2,
    "lagging": 3,
    "healthy": 4,
    "human": 5,
}


def _headline(text: str, limit: int = 240) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _sanitize_error(err: str | None) -> str | None:
    if not err:
        return None
    cleaned = _URL_RE.sub("[url]", " ".join(err.split()))
    return cleaned[:160]


def _msg_filter_clause(board_slug: str | None, agent_handle: str | None) -> tuple[str, list[object]]:
    clauses = ["b.archived_at IS NULL", "m.redacted_at IS NULL"]
    params: list[object] = []
    if board_slug:
        clauses.append("b.slug = ?")
        params.append(board_slug)
    if agent_handle:
        clauses.append(
            "(p.handle = ? OR EXISTS ("
            "SELECT 1 FROM mentions mn JOIN principals mp ON mp.id = mn.principal_id "
            "WHERE mn.message_id = m.id AND mp.handle = ?"
            "))"
        )
        params.extend([agent_handle, agent_handle])
    return " AND ".join(clauses), params


def _load_boards_and_handles(
    conn: sqlite3.Connection, *, cutoff_iso: str
) -> tuple[list[dict[str, Any]], list[str]]:
    board_rows = conn.execute(
        """
        SELECT b.slug, b.name, b.next_seq, b.created_at,
               (SELECT COUNT(*)
                FROM messages m
                WHERE m.board_id = b.id AND m.redacted_at IS NULL AND m.created_at >= ?) AS msgs_window,
               (SELECT COUNT(*)
                FROM issues i
                WHERE i.board_id = b.id AND i.status IN ('open', 'in_progress')) AS open_issues,
               (SELECT COUNT(*)
                FROM memberships mb
                JOIN principals p ON p.id = mb.principal_id
                WHERE mb.board_id = b.id AND p.kind != 'system' AND p.disabled_at IS NULL) AS member_count,
               (SELECT MAX(m.created_at)
                FROM messages m
                WHERE m.board_id = b.id AND m.redacted_at IS NULL) AS last_activity_at
        FROM boards b
        WHERE b.archived_at IS NULL
        ORDER BY COALESCE(last_activity_at, b.created_at) DESC, b.slug ASC
        """,
        (cutoff_iso,),
    ).fetchall()
    boards = [
        {
            "slug": r["slug"],
            "name": r["name"],
            "latest_seq": max(0, int(r["next_seq"]) - 1),
            "msgs_window": int(r["msgs_window"]),
            "open_issues": int(r["open_issues"]),
            "member_count": int(r["member_count"]),
            "last_activity_at": r["last_activity_at"],
            "last_activity_local": format_chicago_time(r["last_activity_at"]),
        }
        for r in board_rows
    ]

    handle_rows = conn.execute(
        """
        SELECT DISTINCT p.handle
        FROM memberships mb
        JOIN boards b ON b.id = mb.board_id
        JOIN principals p ON p.id = mb.principal_id
        WHERE b.archived_at IS NULL AND p.kind != 'system' AND p.disabled_at IS NULL
        ORDER BY p.handle ASC
        """
    ).fetchall()
    return boards, [str(r["handle"]) for r in handle_rows]


def _load_agent_matrix(
    conn: sqlite3.Connection,
    *,
    now_iso: str,
    cutoff_iso: str,
    board_slug: str | None,
    agent_handle: str | None,
    telemetry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    clauses = ["b.archived_at IS NULL", "p.kind != 'system'", "p.disabled_at IS NULL"]
    params: list[object] = []
    if board_slug:
        clauses.append("b.slug = ?")
        params.append(board_slug)
    if agent_handle:
        clauses.append("p.handle = ?")
        params.append(agent_handle)
    where_sql = " AND ".join(clauses)

    member_rows = conn.execute(
        f"""
        SELECT b.id AS board_id, b.slug AS board_slug, b.next_seq,
               p.id AS principal_id, p.handle, p.display_name, p.kind, p.tags,
               p.dnd_until, p.dnd_reason,
               mb.role,
               c.read_seq AS cursor_read_seq,
               c.updated_at AS cursor_updated_at
        FROM memberships mb
        JOIN boards b ON b.id = mb.board_id
        JOIN principals p ON p.id = mb.principal_id
        LEFT JOIN cursors c ON c.board_id = b.id AND c.principal_id = p.id
        WHERE {where_sql}
        ORDER BY b.slug ASC, p.handle ASC
        """,
        params,
    ).fetchall()

    post_stats_rows = conn.execute(
        """
        SELECT m.board_id, m.author_id,
               MAX(m.seq) AS last_post_seq,
               MAX(m.created_at) AS last_post_at,
               SUM(CASE WHEN m.created_at >= ? THEN 1 ELSE 0 END) AS msgs_window
        FROM messages m
        JOIN boards b ON b.id = m.board_id
        WHERE b.archived_at IS NULL AND m.redacted_at IS NULL
        GROUP BY m.board_id, m.author_id
        """,
        (cutoff_iso,),
    ).fetchall()
    post_stats = {(r["board_id"], r["author_id"]): r for r in post_stats_rows}

    sub_rows = conn.execute(
        """
        SELECT s.principal_id, s.boards, s.min_interval_s, s.debounce_s,
               s.active, s.failure_count, s.disabled_reason, s.last_sent_at
        FROM subscriptions s
        JOIN principals p ON p.id = s.principal_id
        WHERE p.kind != 'system' AND p.disabled_at IS NULL
        """
    ).fetchall()
    subs_by_principal: dict[str, list[sqlite3.Row]] = {}
    for s in sub_rows:
        subs_by_principal.setdefault(str(s["principal_id"]), []).append(s)

    matrix: list[dict[str, Any]] = []
    for row in member_rows:
        board_id = str(row["board_id"])
        bslug = str(row["board_slug"])
        pid = str(row["principal_id"])
        latest_seq = max(0, int(row["next_seq"]) - 1)
        read_seq = int(row["cursor_read_seq"]) if row["cursor_read_seq"] is not None else 0
        unread_lag = delivery_store.count_unread_for_principal(conn, board_id, pid, read_seq)
        cursor_pct = 100 if latest_seq == 0 else min(100, round(min(read_seq, latest_seq) * 100 / latest_seq))

        pstats = post_stats.get((board_id, pid))
        last_post_seq = int(pstats["last_post_seq"]) if pstats and pstats["last_post_seq"] is not None else None
        last_post_at = str(pstats["last_post_at"]) if pstats and pstats["last_post_at"] else None
        msgs_window = int(pstats["msgs_window"]) if pstats and pstats["msgs_window"] is not None else 0

        matching_subs = [
            s
            for s in subs_by_principal.get(pid, [])
            if "*" in (b_list := json.loads(s["boards"])) or bslug in b_list
        ]
        sub_count = len(matching_subs)
        active_subs = [s for s in matching_subs if bool(s["active"])]
        active_count = len(active_subs)
        disabled_count = sub_count - active_count
        failure_count = sum(int(s["failure_count"]) for s in matching_subs)
        disabled_reasons = [
            sanitized
            for s in matching_subs
            if not bool(s["active"])
            and s["disabled_reason"]
            and (sanitized := _sanitize_error(str(s["disabled_reason"])))
        ]
        timing_pool = active_subs or matching_subs
        min_interval_s = min((int(s["min_interval_s"]) for s in timing_pool), default=None)
        debounce_s = min((int(s["debounce_s"]) for s in timing_pool), default=None)
        last_sent_at = max((str(s["last_sent_at"]) for s in matching_subs if s["last_sent_at"]), default=None)

        if sub_count > 0 and active_count == 0:
            wh_mode = "disabled"
        elif active_count > 0 and failure_count > 0:
            wh_mode = "failing"
        elif active_count > 0:
            wh_mode = "active"
        elif row["kind"] == "human":
            wh_mode = "human"
        else:
            wh_mode = "no_webhook"

        if wh_mode in ("disabled", "failing", "no_webhook"):
            health = wh_mode
        elif unread_lag > 0:
            health = "lagging"
        else:
            health = "healthy"

        raw_tags = row["tags"] if "tags" in row.keys() and row["tags"] else "[]"
        dnd_raw = format_dnd_status(row, now_iso=now_iso)
        dnd_info = {
            **dnd_raw,
            "until_local": format_chicago_time(dnd_raw["until"]) if dnd_raw["active"] else None,
        }
        tel_agents = (telemetry or {}).get("agents") or {}
        tel_agent = tel_agents.get(str(row["handle"]))
        tel_summary = (
            {
                "live_context_tokens": int(tel_agent.get("live_context_tokens", 0)),
                "uncached_5h": int(tel_agent.get("uncached_5h", 0)),
                "hit_rate_5h": tel_agent.get("hit_rate_5h"),
                "partial_collapses_5h": int(tel_agent.get("partial_collapses_5h", 0)),
                "alerts": list(tel_agent.get("alerts") or []),
            }
            if isinstance(tel_agent, dict)
            else None
        )
        matrix.append(
            {
                "board_slug": bslug,
                "handle": row["handle"],
                "display_name": row["display_name"],
                "kind": row["kind"],
                "role": row["role"],
                "tags": json.loads(raw_tags),
                "dnd": dnd_info,
                "telemetry": tel_summary,
                "read_seq": read_seq,
                "latest_seq": latest_seq,
                "unread_lag": unread_lag,
                "cursor_pct": cursor_pct,
                "cursor_updated_at": row["cursor_updated_at"],
                "cursor_updated_local": format_chicago_time(row["cursor_updated_at"]),
                "last_post_seq": last_post_seq,
                "last_post_at": last_post_at,
                "last_post_local": format_chicago_time(last_post_at),
                "msgs_window": msgs_window,
                "health": health,
                "webhook": {
                    "mode": wh_mode,
                    "subscription_count": sub_count,
                    "active_count": active_count,
                    "disabled_count": disabled_count,
                    "failure_count": failure_count,
                    "disabled_reasons": disabled_reasons,
                    "min_interval_s": min_interval_s,
                    "debounce_s": debounce_s,
                    "last_sent_at": last_sent_at,
                    "last_sent_local": format_chicago_time(last_sent_at),
                },
            }
        )

    matrix.sort(key=lambda r: (_HEALTH_ORDER.get(r["health"], 9), -r["unread_lag"], r["board_slug"], r["handle"]))
    return matrix


def _build_timeline_grid(now: datetime, hours: int) -> tuple[datetime, int, list[dict[str, Any]]]:
    step_hours = 1 if hours <= 48 else 6
    cutoff_dt = now - timedelta(hours=hours)
    if step_hours == 1:
        start_dt = cutoff_dt.replace(minute=0, second=0, microsecond=0)
        end_dt = now.replace(minute=0, second=0, microsecond=0)
    else:
        start_dt = cutoff_dt.replace(hour=(cutoff_dt.hour // 6) * 6, minute=0, second=0, microsecond=0)
        end_dt = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)

    buckets: list[dict[str, Any]] = []
    cur = start_dt
    fmt = "%H:00" if hours <= 24 else "%m-%d %Hh"
    while cur <= end_dt:
        buckets.append(
            {
                "bucket_start": dt_to_iso(cur),
                "label": cur.astimezone(_CHICAGO).strftime(fmt),
                "messages": 0,
                "wakes_sent": 0,
                "wakes_suppressed": 0,
            }
        )
        cur += timedelta(hours=step_hours)
    return start_dt, step_hours * 3600, buckets


def _load_timeline_and_metrics(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    hours: int,
    cutoff_iso: str,
    board_slug: str | None,
    agent_handle: str | None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    start_dt, step_secs, buckets = _build_timeline_grid(now, hours)
    msg_where, msg_params = _msg_filter_clause(board_slug, agent_handle)
    msg_rows = conn.execute(
        f"""
        SELECT m.created_at,
               NOT EXISTS (
                   SELECT 1 FROM mentions mn WHERE mn.message_id = m.id AND mn.role = 'to'
               ) AS no_to,
               EXISTS (
                   SELECT 1 FROM mentions mn WHERE mn.message_id = m.id AND mn.role = 'cc'
               ) AS has_cc
        FROM messages m
        JOIN boards b ON b.id = m.board_id
        JOIN principals p ON p.id = m.author_id
        WHERE {msg_where} AND m.created_at >= ?
        """,
        [*msg_params, cutoff_iso],
    ).fetchall()

    silent_messages_window = 0
    cc_messages_window = 0
    for r in msg_rows:
        if bool(r["no_to"]):
            silent_messages_window += 1
        if bool(r["has_cc"]):
            cc_messages_window += 1
        idx = int((iso_to_dt(str(r["created_at"])) - start_dt).total_seconds() // step_secs)
        if 0 <= idx < len(buckets):
            buckets[idx]["messages"] += 1

    outbox_clauses = ["b.archived_at IS NULL"]
    outbox_params: list[object] = []
    if board_slug:
        outbox_clauses.append("b.slug = ?")
        outbox_params.append(board_slug)
    if agent_handle:
        outbox_clauses.append("p.handle = ?")
        outbox_params.append(agent_handle)
    outbox_where = " AND ".join(outbox_clauses)

    outbox_rows = conn.execute(
        f"""
        SELECT o.status, COALESCE(o.sent_at, o.created_at) AS event_at
        FROM outbox o
        JOIN boards b ON b.id = o.board_id
        JOIN subscriptions s ON s.id = o.subscription_id
        JOIN principals p ON p.id = s.principal_id
        WHERE {outbox_where}
          AND (o.status IN ('pending', 'sending') OR COALESCE(o.sent_at, o.created_at) >= ?)
        """,
        [*outbox_params, cutoff_iso],
    ).fetchall()

    wakes_sent_window = 0
    wakes_suppressed_window = 0
    wakes_pending = 0
    wakes_dead_window = 0
    for r in outbox_rows:
        st = str(r["status"])
        if st in ("pending", "sending"):
            wakes_pending += 1
            continue
        ev_at = str(r["event_at"])
        if ev_at < cutoff_iso:
            continue
        idx = int((iso_to_dt(ev_at) - start_dt).total_seconds() // step_secs)
        if st == "sent":
            wakes_sent_window += 1
            if 0 <= idx < len(buckets):
                buckets[idx]["wakes_sent"] += 1
        elif st == "suppressed":
            wakes_suppressed_window += 1
            if 0 <= idx < len(buckets):
                buckets[idx]["wakes_suppressed"] += 1
        elif st == "dead":
            wakes_dead_window += 1

    metrics = {
        "messages_window": len(msg_rows),
        "silent_messages_window": silent_messages_window,
        "cc_messages_window": cc_messages_window,
        "wakes_sent_window": wakes_sent_window,
        "wakes_suppressed_window": wakes_suppressed_window,
        "wakes_pending": wakes_pending,
        "wakes_dead_window": wakes_dead_window,
    }
    return buckets, metrics


def _load_interactions(
    conn: sqlite3.Connection,
    *,
    cutoff_iso: str,
    board_slug: str | None,
    agent_handle: str | None,
) -> list[dict[str, Any]]:
    clauses = [
        "b.archived_at IS NULL",
        "m.redacted_at IS NULL",
        "m.created_at >= ?",
        "m.author_id != mn.principal_id",
        "pa.kind != 'system'",
        "pr.kind != 'system'",
    ]
    params: list[object] = [cutoff_iso]
    if board_slug:
        clauses.append("b.slug = ?")
        params.append(board_slug)
    if agent_handle:
        clauses.append("(pa.handle = ? OR pr.handle = ?)")
        params.extend([agent_handle, agent_handle])
    where_sql = " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT pa.handle AS from_handle,
               pr.handle AS to_handle,
               mn.role AS role,
               b.slug AS board_slug
        FROM mentions mn
        JOIN messages m ON m.id = mn.message_id
        JOIN boards b ON b.id = m.board_id
        JOIN principals pa ON pa.id = m.author_id
        JOIN principals pr ON pr.id = mn.principal_id
        WHERE {where_sql}
        """,
        params,
    ).fetchall()

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (str(r["from_handle"]), str(r["to_handle"]))
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "from_handle": key[0],
                "to_handle": key[1],
                "to_count": 0,
                "cc_count": 0,
                "total": 0,
                "boards": set(),
            }
            grouped[key] = entry
        if r["role"] == "cc":
            entry["cc_count"] += 1
        else:
            entry["to_count"] += 1
        entry["total"] += 1
        entry["boards"].add(str(r["board_slug"]))

    result = [
        {**item, "boards": sorted(item["boards"])}
        for item in grouped.values()
    ]
    result.sort(key=lambda x: (-x["total"], -x["to_count"], x["from_handle"], x["to_handle"]))
    return result[:25]


def _load_open_issues(
    conn: sqlite3.Connection,
    *,
    board_slug: str | None,
    agent_handle: str | None,
) -> tuple[list[dict[str, Any]], int]:
    clauses = ["b.archived_at IS NULL", "i.status IN ('open', 'in_progress')"]
    params: list[object] = []
    if board_slug:
        clauses.append("b.slug = ?")
        params.append(board_slug)
    if agent_handle:
        clauses.append("(asg.handle = ? OR op.handle = ?)")
        params.extend([agent_handle, agent_handle])
    where_sql = " AND ".join(clauses)

    total_row = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt
        FROM issues i
        JOIN boards b ON b.id = i.board_id
        JOIN principals op ON op.id = i.opened_by
        LEFT JOIN principals asg ON asg.id = i.assignee_id
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    total = int(total_row["cnt"]) if total_row else 0

    rows = conn.execute(
        f"""
        SELECT b.slug AS board_slug, i.number, i.title, i.severity, i.status,
               i.opened_at, i.thread_id, asg.handle AS assignee_handle
        FROM issues i
        JOIN boards b ON b.id = i.board_id
        JOIN principals op ON op.id = i.opened_by
        LEFT JOIN principals asg ON asg.id = i.assignee_id
        WHERE {where_sql}
        ORDER BY CASE i.severity
                     WHEN 'blocker' THEN 0
                     WHEN 'high' THEN 1
                     WHEN 'normal' THEN 2
                     ELSE 3
                 END,
                 i.opened_at DESC
        LIMIT 25
        """,
        params,
    ).fetchall()
    return [
        {
            "board_slug": r["board_slug"],
            "ref": f"I-{r['number']}",
            "title": r["title"],
            "severity": r["severity"],
            "status": r["status"],
            "assignee_handle": r["assignee_handle"],
            "opened_at": r["opened_at"],
            "opened_at_local": format_chicago_time(r["opened_at"]),
            "thread_id": r["thread_id"],
        }
        for r in rows
    ], total


def _load_recent_outbox(
    conn: sqlite3.Connection,
    *,
    board_slug: str | None,
    agent_handle: str | None,
) -> list[dict[str, Any]]:
    clauses = ["b.archived_at IS NULL"]
    params: list[object] = []
    if board_slug:
        clauses.append("b.slug = ?")
        params.append(board_slug)
    if agent_handle:
        clauses.append("p.handle = ?")
        params.append(agent_handle)
    where_sql = " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT b.slug AS board_slug, p.handle AS principal_handle,
               o.status, o.latest_seq, o.attempt, o.created_at, o.sent_at,
               o.next_attempt_at, o.last_error
        FROM outbox o
        JOIN boards b ON b.id = o.board_id
        JOIN subscriptions s ON s.id = o.subscription_id
        JOIN principals p ON p.id = s.principal_id
        WHERE {where_sql}
        ORDER BY COALESCE(o.sent_at, o.created_at) DESC, o.id DESC
        LIMIT 25
        """,
        params,
    ).fetchall()
    return [
        {
            "board_slug": r["board_slug"],
            "principal_handle": r["principal_handle"],
            "status": r["status"],
            "latest_seq": int(r["latest_seq"]),
            "attempt": int(r["attempt"]),
            "created_at": r["created_at"],
            "created_at_local": format_chicago_time(r["created_at"]),
            "sent_at": r["sent_at"],
            "sent_at_local": format_chicago_time(r["sent_at"]),
            "next_attempt_at": r["next_attempt_at"],
            "last_error": _sanitize_error(r["last_error"]),
        }
        for r in rows
    ]


def _load_feed(
    conn: sqlite3.Connection,
    *,
    board_slug: str | None,
    agent_handle: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    where_sql, params = _msg_filter_clause(board_slug, agent_handle)
    rows = conn.execute(
        f"""
        SELECT m.id, m.seq, m.type, m.priority, m.body_md, m.created_at, m.thread_id,
               b.slug AS board_slug, t.title AS thread_title, p.handle AS author_handle
        FROM messages m
        JOIN boards b ON b.id = m.board_id
        JOIN threads t ON t.id = m.thread_id
        JOIN principals p ON p.id = m.author_id
        WHERE {where_sql}
        ORDER BY m.created_at DESC, m.seq DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    msg_ids = [str(r["id"]) for r in rows]
    to_map, cc_map = messages_store.get_recipients_for_messages(conn, msg_ids)
    return [
        {
            "id": r["id"],
            "board_slug": r["board_slug"],
            "thread_id": r["thread_id"],
            "thread_title": r["thread_title"],
            "seq": int(r["seq"]),
            "author_handle": r["author_handle"],
            "type": r["type"],
            "priority": r["priority"] if "priority" in r.keys() and r["priority"] else "normal",
            "to": to_map.get(r["id"], []),
            "cc": cc_map.get(r["id"], []),
            "body_preview": _headline(r["body_md"]),
            "created_at": r["created_at"],
            "created_at_local": format_chicago_time(r["created_at"]),
        }
        for r in rows
    ]


def get_monitor_snapshot(
    services: AppServices,
    ctx: AuthContext,
    *,
    hours: int = 24,
    board_slug: str | None = None,
    agent_handle: str | None = None,
    limit: int = 60,
) -> dict[str, Any]:
    """Return a non-secret live cross-board observability snapshot for server owners."""
    require_server_owner(ctx)
    if hours < 1 or hours > 168:
        raise ValidationError("hours must be between 1 and 168")
    if limit < 1 or limit > 200:
        raise ValidationError("limit must be between 1 and 200")

    clean_board = board_slug.strip() or None if board_slug else None
    clean_agent = agent_handle.strip().lstrip("@").lower() or None if agent_handle else None
    now = utc_now()
    now_iso = dt_to_iso(now)
    cutoff_iso = dt_to_iso(now - timedelta(hours=hours))

    with services.db.read_snapshot() as conn:
        tel_rows = telemetry_store.list_telemetry_snapshots(conn)
        telemetry: dict[str, Any] | None = None
        if tel_rows:
            tel_payload = json.loads(tel_rows[0]["payload_json"])
            telemetry = {
                "host_id": tel_rows[0]["host_id"],
                "reported_at": tel_rows[0]["reported_at"],
                "reported_at_local": format_chicago_time(tel_rows[0]["reported_at"]),
                "thresholds": tel_payload.get("thresholds", {}),
                "global": tel_payload.get("global", {}),
                "alerts": tel_payload.get("alerts", []),
                "agents": tel_payload.get("agents", {}),
            }

        boards, handles = _load_boards_and_handles(conn, cutoff_iso=cutoff_iso)
        agent_matrix = _load_agent_matrix(
            conn,
            now_iso=now_iso,
            cutoff_iso=cutoff_iso,
            board_slug=clean_board,
            agent_handle=clean_agent,
            telemetry=telemetry,
        )
        timeline, metrics = _load_timeline_and_metrics(
            conn,
            now=now,
            hours=hours,
            cutoff_iso=cutoff_iso,
            board_slug=clean_board,
            agent_handle=clean_agent,
        )
        interactions = _load_interactions(
            conn, cutoff_iso=cutoff_iso, board_slug=clean_board, agent_handle=clean_agent
        )
        open_issues, open_issues_total = _load_open_issues(
            conn, board_slug=clean_board, agent_handle=clean_agent
        )
        recent_outbox = _load_recent_outbox(conn, board_slug=clean_board, agent_handle=clean_agent)
        feed = _load_feed(conn, board_slug=clean_board, agent_handle=clean_agent, limit=limit)

    matching_boards = [b for b in boards if not clean_board or b["slug"] == clean_board]
    active_agents = {r["handle"] for r in agent_matrix if r["kind"] == "agent"}
    lagging_agents = sum(1 for r in agent_matrix if r["unread_lag"] > 0)
    unsubscribed_agents = len(
        {r["handle"] for r in agent_matrix if r["webhook"]["mode"] == "no_webhook"}
    )
    failing_or_disabled = len(
        {r["handle"] for r in agent_matrix if r["webhook"]["mode"] in ("failing", "disabled")}
    )

    summary = {
        "active_boards": len(matching_boards),
        "active_agents": len(active_agents),
        **metrics,
        "open_issues_total": open_issues_total,
        "lagging_agents": lagging_agents,
        "unsubscribed_agents": unsubscribed_agents,
        "failing_or_disabled_webhooks": failing_or_disabled,
    }

    return {
        "generated_at": now_iso,
        "generated_at_local": format_chicago_time(now_iso),
        "window_hours": hours,
        "filters": {"board": clean_board, "agent": clean_agent},
        "summary": summary,
        "boards": boards,
        "handles": handles,
        "agent_matrix": agent_matrix,
        "timeline": timeline,
        "interactions": interactions,
        "open_issues": open_issues,
        "recent_outbox": recent_outbox,
        "feed": feed,
        "telemetry": telemetry,
    }
