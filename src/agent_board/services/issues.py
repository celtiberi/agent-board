"""Domain service for Issues lifecycle, events, and retest linkage."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_board.errors import ConflictError, NotFoundError, ValidationError
from agent_board.ids import new_ulid, parse_issue_ref, utc_now_iso
from agent_board.services.auth import record_audit, require_board_role
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.idempotency import execute_idempotent
from agent_board.services.messages import post_message_in_tx
from agent_board.store import boards as boards_store
from agent_board.store import issues as issues_store
from agent_board.store import principals as principals_store

VALID_SEVERITIES = ("blocker", "high", "normal", "low")
SEVERITY_ALIASES = {"medium": "normal", "med": "normal", "critical": "blocker", "urgent": "blocker", "minor": "low"}
VALID_ISSUE_STATUSES = ("open", "in_progress", "resolved", "wont_fix")
CLOSED_ISSUE_STATUSES = ("resolved", "wont_fix")


def _resolve_assignee(conn: sqlite3.Connection, assignee: str | None) -> sqlite3.Row | None:
    if assignee is None or not assignee.strip():
        return None
    handle = assignee.strip().lstrip("@").lower()
    row = principals_store.get_principal_by_handle(conn, handle)
    if row is None:
        row = principals_store.get_principal_by_id(conn, assignee.strip())
    if row is None or row["kind"] == "system" or row["disabled_at"] is not None:
        raise NotFoundError(f"Assignee principal '{assignee}' not found")
    return row


def _latest_retests_by_issue(conn: sqlite3.Connection, board_id: str) -> dict[tuple[str, int], dict[str, Any]]:
    reports = issues_store.list_retest_reports_for_board(conn, board_id)
    mapping: dict[tuple[str, int], dict[str, Any]] = {}
    for rep in reports:
        if not rep["data"]:
            continue
        data = json.loads(rep["data"])
        for raw_ref in data.get("issue_ids") or []:
            kind, val = parse_issue_ref(str(raw_ref))
            key: tuple[str, int] | None = None
            if kind == "number" and isinstance(val, int):
                key = ("number", val)
            elif kind == "id" and isinstance(val, str):
                # Also map by ULID key via negative/string handled separately
                key = (val, -1)
            if key and key not in mapping:
                mapping[key] = {
                    "message_id": rep["id"],
                    "seq": int(rep["seq"]),
                    "target": data.get("target"),
                    "build": data.get("build"),
                    "passed": data.get("passed"),
                    "failed": data.get("failed"),
                    "author_handle": rep["author_handle"],
                    "created_at": rep["created_at"],
                }
    return mapping


def issue_row_to_dict(
    row: sqlite3.Row,
    *,
    retest_map: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    latest_retest = None
    if retest_map:
        latest_retest = retest_map.get(("number", int(row["number"]))) or retest_map.get((row["id"], -1))
    return {
        "id": row["id"],
        "ref": f"I-{row['number']}",
        "board_id": row["board_id"],
        "number": int(row["number"]),
        "title": row["title"],
        "body_md": row["body_md"],
        "severity": row["severity"],
        "status": row["status"],
        "assignee_id": row["assignee_id"],
        "assignee_handle": row["assignee_handle"],
        "opened_by": row["opened_by"],
        "opened_by_handle": row["opened_by_handle"],
        "opened_at": row["opened_at"],
        "resolved_at": row["resolved_at"],
        "resolution": row["resolution"],
        "thread_id": row["thread_id"],
        "tags": json.loads(row["tags"]),
        "updated_at": row["updated_at"],
        "latest_retest": latest_retest,
    }


def open_issue(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    title: str,
    body_md: str,
    severity: str = "normal",
    assignee: str | None = None,
    tags: list[str] | None = None,
    thread_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    clean_title = title.strip()
    clean_body = body_md.strip()
    if not clean_title or not clean_body:
        raise ValidationError("title and body_md are required")
    norm_severity = severity.strip().lower()
    clean_severity = SEVERITY_ALIASES.get(norm_severity, norm_severity)
    if clean_severity not in VALID_SEVERITIES:
        raise ValidationError(f"severity must be one of {', '.join(VALID_SEVERITIES)}")
    clean_tags = [t.strip().lower() for t in (tags or []) if t.strip()]

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent")
        assignee_row = _resolve_assignee(conn, assignee)

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            num = boards_store.allocate_next_issue_num(conn, board["id"])
            ref = f"I-{num}"
            issue_id = new_ulid()

            if thread_id:
                thread = boards_store.get_thread_by_id(conn, thread_id)
                if thread is None or thread["board_id"] != board["id"]:
                    raise NotFoundError(f"Thread '{thread_id}' not found on this board")
                if thread["closed_at"] is not None:
                    raise ValidationError("Thread is closed", code="thread_closed")
                target_tid = thread["id"]
            else:
                target_tid = new_ulid()
                boards_store.insert_thread(
                    conn,
                    thread_id=target_tid,
                    board_id=board["id"],
                    title=f"{ref}: {clean_title}",
                    created_by=ctx.principal_id,
                    created_at=now_iso,
                )

            assignee_note = f" · assigned to @{assignee_row['handle']}" if assignee_row is not None else ""
            msg = post_message_in_tx(
                conn,
                services=services,
                ctx=ctx,
                board=board,
                thread_id=target_tid,
                msg_type="issue",
                body_md=f"**Opened {ref} [{clean_severity}]: {clean_title}**{assignee_note}\n\n{clean_body}",
                data={"issue_id": ref, "action": "opened"},
                now_iso=now_iso,
            )
            issue_rowid = issues_store.insert_issue(
                conn,
                issue_id=issue_id,
                board_id=board["id"],
                number=num,
                title=clean_title,
                body_md=clean_body,
                severity=clean_severity,
                assignee_id=assignee_row["id"] if assignee_row else None,
                opened_by=ctx.principal_id,
                thread_id=target_tid,
                tags_json=json.dumps(clean_tags),
                now_iso=now_iso,
            )
            issues_store.insert_issue_event(
                conn,
                event_id=new_ulid(),
                issue_id=issue_id,
                action="opened",
                from_val=None,
                to_val="open",
                actor_id=ctx.principal_id,
                message_id=msg["id"],
                at=now_iso,
            )
            issues_store.upsert_issue_fts(
                conn,
                issue_rowid=issue_rowid,
                issue_id=issue_id,
                board_id=board["id"],
                title=clean_title,
                body_md=clean_body,
                resolution=None,
            )
            record_audit(
                conn,
                ctx=ctx,
                action="issue.open",
                ok=True,
                board_id=board["id"],
                target=ref,
            )
            row = issues_store.get_issue_by_ref(conn, board["id"], "id", issue_id)
            assert row is not None
            return issue_row_to_dict(row)

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"issue.open:{board['slug']}",
            request_payload={
                "title": clean_title,
                "body_md": clean_body,
                "severity": severity,
                "assignee": assignee_row["handle"] if assignee_row else None,
                "tags": clean_tags,
                "thread_id": thread_id,
            },
            status_code=201,
            fn=_do,
        )
        return res


def update_issue(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    ref: str,
    status: str | None = None,
    assignee: str | None = None,
    comment: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if status is None and assignee is None and (comment is None or not comment.strip()):
        raise ValidationError("Provide at least one of status, assignee, or comment")

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent")
        ref_kind, ref_val = parse_issue_ref(ref)
        issue = issues_store.get_issue_by_ref(conn, board["id"], ref_kind, ref_val)
        if issue is None:
            raise NotFoundError(f"Issue '{ref}' not found on board '{board_slug}'")

        if status is not None:
            if status not in VALID_ISSUE_STATUSES:
                raise ValidationError(f"Invalid issue status '{status}'")
            if status in CLOSED_ISSUE_STATUSES:
                raise ValidationError("Use close_issue with a resolution (>= 20 chars) to close an issue")
            if issue["status"] in CLOSED_ISSUE_STATUSES:
                raise ValidationError(
                    f"Issue I-{issue['number']} is '{issue['status']}'; use reopen_issue to reopen it"
                )

        new_assignee_id = issue["assignee_id"]
        new_assignee_handle = issue["assignee_handle"]
        assignee_changed = False
        if assignee is not None:
            if assignee == "":
                if new_assignee_id is not None:
                    assignee_changed = True
                new_assignee_id = None
                new_assignee_handle = None
            else:
                arow = _resolve_assignee(conn, assignee)
                assert arow is not None
                if arow["id"] != new_assignee_id:
                    assignee_changed = True
                new_assignee_id = arow["id"]
                new_assignee_handle = arow["handle"]

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            label = f"I-{issue['number']}"
            next_status = status if status is not None else issue["status"]
            status_changed = status is not None and status != issue["status"]

            parts: list[str] = [f"**Update {label}**"]
            action = "commented"
            if status_changed:
                action = "status_changed"
                parts.append(f"Status: `{issue['status']}` → `{next_status}`")
            if assignee_changed:
                if not status_changed:
                    action = "assigned"
                target_txt = f"@{new_assignee_handle}" if new_assignee_handle else "unassigned"
                parts.append(f"Assignee: {target_txt}")
            if comment and comment.strip():
                parts.append(comment.strip())

            msg = post_message_in_tx(
                conn,
                services=services,
                ctx=ctx,
                board=board,
                thread_id=issue["thread_id"],
                msg_type="issue",
                body_md="\n\n".join(parts),
                data={"issue_id": label, "action": action},
                now_iso=now_iso,
            )
            issues_store.update_issue_fields(
                conn,
                issue_id=issue["id"],
                status=next_status,
                assignee_id=new_assignee_id,
                resolved_at=issue["resolved_at"],
                resolution=issue["resolution"],
                updated_at=now_iso,
            )
            if status_changed:
                issues_store.insert_issue_event(
                    conn,
                    event_id=new_ulid(),
                    issue_id=issue["id"],
                    action="status_changed",
                    from_val=issue["status"],
                    to_val=next_status,
                    actor_id=ctx.principal_id,
                    message_id=msg["id"],
                    at=now_iso,
                )
            if assignee_changed:
                issues_store.insert_issue_event(
                    conn,
                    event_id=new_ulid(),
                    issue_id=issue["id"],
                    action="assigned",
                    from_val=issue["assignee_handle"],
                    to_val=new_assignee_handle,
                    actor_id=ctx.principal_id,
                    message_id=msg["id"],
                    at=now_iso,
                )
            if comment and comment.strip() and not status_changed and not assignee_changed:
                issues_store.insert_issue_event(
                    conn,
                    event_id=new_ulid(),
                    issue_id=issue["id"],
                    action="commented",
                    from_val=None,
                    to_val=comment.strip()[:200],
                    actor_id=ctx.principal_id,
                    message_id=msg["id"],
                    at=now_iso,
                )
            record_audit(
                conn,
                ctx=ctx,
                action="issue.update",
                ok=True,
                board_id=board["id"],
                target=label,
            )
            updated = issues_store.get_issue_by_ref(conn, board["id"], "id", issue["id"])
            assert updated is not None
            retests = _latest_retests_by_issue(conn, board["id"])
            return issue_row_to_dict(updated, retest_map=retests)

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"issue.update:{board['slug']}:{issue['id']}",
            request_payload={"status": status, "assignee": assignee, "comment": comment},
            status_code=200,
            fn=_do,
        )
        return res


def close_issue(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    ref: str,
    resolution: str,
    status: str = "resolved",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if status not in CLOSED_ISSUE_STATUSES:
        raise ValidationError("status must be 'resolved' or 'wont_fix'")
    clean_res = resolution.strip()
    if len(clean_res) < 20:
        raise ValidationError(
            "resolution must be at least 20 characters explaining the structural cause and fix",
            code="resolution_too_short",
        )

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent")
        ref_kind, ref_val = parse_issue_ref(ref)
        issue = issues_store.get_issue_by_ref(conn, board["id"], ref_kind, ref_val)
        if issue is None:
            raise NotFoundError(f"Issue '{ref}' not found on board '{board_slug}'")
        if issue["status"] in CLOSED_ISSUE_STATUSES:
            raise ConflictError(f"Issue I-{issue['number']} is already '{issue['status']}'")

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            label = f"I-{issue['number']}"
            msg = post_message_in_tx(
                conn,
                services=services,
                ctx=ctx,
                board=board,
                thread_id=issue["thread_id"],
                msg_type="issue",
                body_md=f"**Closed {label} as `{status}`**\n\n_Resolution:_ {clean_res}",
                data={"issue_id": label, "action": "resolved"},
                now_iso=now_iso,
            )
            issues_store.update_issue_fields(
                conn,
                issue_id=issue["id"],
                status=status,
                assignee_id=issue["assignee_id"],
                resolved_at=now_iso,
                resolution=clean_res,
                updated_at=now_iso,
            )
            issues_store.insert_issue_event(
                conn,
                event_id=new_ulid(),
                issue_id=issue["id"],
                action="resolved",
                from_val=issue["status"],
                to_val=status,
                actor_id=ctx.principal_id,
                message_id=msg["id"],
                at=now_iso,
            )
            issues_store.upsert_issue_fts(
                conn,
                issue_rowid=int(issue["_rowid"]),
                issue_id=issue["id"],
                board_id=board["id"],
                title=issue["title"],
                body_md=issue["body_md"],
                resolution=clean_res,
            )
            record_audit(
                conn,
                ctx=ctx,
                action="issue.close",
                ok=True,
                board_id=board["id"],
                target=label,
                detail={"status": status},
            )
            updated = issues_store.get_issue_by_ref(conn, board["id"], "id", issue["id"])
            assert updated is not None
            retests = _latest_retests_by_issue(conn, board["id"])
            return issue_row_to_dict(updated, retest_map=retests)

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"issue.close:{board['slug']}:{issue['id']}",
            request_payload={"resolution": clean_res, "status": status},
            status_code=200,
            fn=_do,
        )
        return res


def reopen_issue(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    ref: str,
    reason: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValidationError("Reopen reason is required")

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "agent")
        ref_kind, ref_val = parse_issue_ref(ref)
        issue = issues_store.get_issue_by_ref(conn, board["id"], ref_kind, ref_val)
        if issue is None:
            raise NotFoundError(f"Issue '{ref}' not found on board '{board_slug}'")
        if issue["status"] not in CLOSED_ISSUE_STATUSES:
            raise ConflictError(f"Issue I-{issue['number']} is already '{issue['status']}'")

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            label = f"I-{issue['number']}"
            msg = post_message_in_tx(
                conn,
                services=services,
                ctx=ctx,
                board=board,
                thread_id=issue["thread_id"],
                msg_type="issue",
                body_md=f"**Reopened {label}** (was `{issue['status']}`)\n\n_Reason:_ {clean_reason}",
                data={"issue_id": label, "action": "reopened"},
                now_iso=now_iso,
            )
            issues_store.update_issue_fields(
                conn,
                issue_id=issue["id"],
                status="open",
                assignee_id=issue["assignee_id"],
                resolved_at=None,
                resolution=None,
                updated_at=now_iso,
            )
            issues_store.insert_issue_event(
                conn,
                event_id=new_ulid(),
                issue_id=issue["id"],
                action="reopened",
                from_val=issue["status"],
                to_val="open",
                actor_id=ctx.principal_id,
                message_id=msg["id"],
                at=now_iso,
            )
            issues_store.upsert_issue_fts(
                conn,
                issue_rowid=int(issue["_rowid"]),
                issue_id=issue["id"],
                board_id=board["id"],
                title=issue["title"],
                body_md=issue["body_md"],
                resolution=None,
            )
            record_audit(
                conn,
                ctx=ctx,
                action="issue.reopen",
                ok=True,
                board_id=board["id"],
                target=label,
                detail={"reason": clean_reason},
            )
            updated = issues_store.get_issue_by_ref(conn, board["id"], "id", issue["id"])
            assert updated is not None
            retests = _latest_retests_by_issue(conn, board["id"])
            return issue_row_to_dict(updated, retest_map=retests)

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"issue.reopen:{board['slug']}:{issue['id']}",
            request_payload={"reason": clean_reason},
            status_code=200,
            fn=_do,
        )
        return res


def list_issues(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    status: str = "active",
    severity: str | None = None,
) -> list[dict[str, Any]]:
    if status not in ("active", "all", *VALID_ISSUE_STATUSES):
        raise ValidationError("Invalid status filter")
    if severity and severity not in ("all", *VALID_SEVERITIES):
        raise ValidationError("Invalid severity filter")

    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    rows = issues_store.list_issues(conn, board["id"], status=status, severity=severity)
    retests = _latest_retests_by_issue(conn, board["id"])
    return [issue_row_to_dict(r, retest_map=retests) for r in rows]


def get_issue_detail(services: AppServices, ctx: AuthContext, *, board_slug: str, ref: str) -> dict[str, Any]:
    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    ref_kind, ref_val = parse_issue_ref(ref)
    issue = issues_store.get_issue_by_ref(conn, board["id"], ref_kind, ref_val)
    if issue is None:
        raise NotFoundError(f"Issue '{ref}' not found on board '{board_slug}'")
    events = issues_store.list_issue_events(conn, issue["id"])
    retests = _latest_retests_by_issue(conn, board["id"])
    return {
        "issue": issue_row_to_dict(issue, retest_map=retests),
        "events": [
            {
                "id": e["id"],
                "action": e["action"],
                "from": e["from_val"],
                "to": e["to_val"],
                "actor_handle": e["actor_handle"],
                "message_id": e["message_id"],
                "at": e["at"],
            }
            for e in events
        ],
    }
