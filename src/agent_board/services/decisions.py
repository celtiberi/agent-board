"""Domain service for versioned Decisions log and DECISIONS.md export."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_board.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from agent_board.ids import new_ulid, parse_decision_ref, utc_now_iso, validate_iso_date
from agent_board.services.auth import record_audit, require_board_role
from agent_board.services.boards import _parse_settings
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.idempotency import execute_idempotent
from agent_board.services.messages import post_message_in_tx
from agent_board.store import boards as boards_store
from agent_board.store import decisions as decisions_store


def _require_decision_write_permission(board: sqlite3.Row, eff_role: str) -> None:
    if eff_role == "owner":
        return
    if eff_role != "agent":
        raise PermissionDeniedError("Insufficient role to record or supersede decisions")
    settings = _parse_settings(board["settings_json"])
    if not settings.get("agents_may_record_decisions", True):
        raise PermissionDeniedError(
            "Only board owners may record decisions on this board",
            code="agents_cannot_record_decisions",
        )


def decision_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    sup_num = row["superseded_by_number"] if "superseded_by_number" in row.keys() else None
    return {
        "id": row["id"],
        "ref": f"D-{row['number']}",
        "board_id": row["board_id"],
        "number": int(row["number"]),
        "title": row["title"],
        "statement": row["statement"],
        "rationale": row["rationale"],
        "decided_by": row["decided_by"],
        "decided_on": row["decided_on"],
        "recorded_by": row["recorded_by"],
        "recorded_by_handle": row["recorded_by_handle"],
        "status": row["status"],
        "superseded_by": row["superseded_by"],
        "superseded_by_ref": f"D-{sup_num}" if sup_num is not None else None,
        "tags": json.loads(row["tags"]),
        "source_message_id": row["source_message_id"],
        "version": int(row["version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _snapshot_payload(row: sqlite3.Row) -> str:
    return json.dumps(
        {
            "id": row["id"],
            "number": int(row["number"]),
            "title": row["title"],
            "statement": row["statement"],
            "rationale": row["rationale"],
            "decided_by": row["decided_by"],
            "decided_on": row["decided_on"],
            "status": row["status"],
            "superseded_by": row["superseded_by"],
            "tags": json.loads(row["tags"]),
        },
        sort_keys=True,
    )


def record_decision(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    title: str,
    statement: str,
    decided_by: str,
    decided_on: str,
    rationale: str | None = None,
    tags: list[str] | None = None,
    thread_title: str = "decisions",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    clean_title = title.strip()
    clean_statement = statement.strip()
    clean_decided_by = decided_by.strip()
    if not clean_title or not clean_statement or not clean_decided_by:
        raise ValidationError("title, statement, and decided_by are required")
    try:
        clean_date = validate_iso_date(decided_on.strip())
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    clean_tags = [t.strip().lower() for t in (tags or []) if t.strip()]
    raw_rationale = rationale.strip() if rationale and rationale.strip() else None

    clean_title, title_changed = services.redactor.scrub_text(clean_title)
    clean_statement, stmt_changed = services.redactor.scrub_text(clean_statement)
    clean_thread, thread_changed = services.redactor.scrub_text(thread_title or "decisions")
    clean_rationale: str | None = None
    rat_changed = False
    if raw_rationale is not None:
        clean_rationale, rat_changed = services.redactor.scrub_text(raw_rationale)
    pre_redacted = title_changed or stmt_changed or thread_changed or rat_changed

    with services.db.write_tx() as conn:
        board, eff_role = require_board_role(conn, ctx, board_slug, "agent")
        _require_decision_write_permission(board, eff_role)

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            num = boards_store.allocate_next_decision_num(conn, board["id"])
            ref = f"D-{num}"
            dec_id = new_ulid()
            body_parts = [f"**{ref}: {clean_title}**", "", clean_statement]
            if clean_rationale:
                body_parts.extend(["", f"_Rationale:_ {clean_rationale}"])
            body_parts.append(f"\n_Decided by {clean_decided_by} on {clean_date} (recorded by {ctx.handle})_")

            msg = post_message_in_tx(
                conn,
                services=services,
                ctx=ctx,
                board=board,
                thread_title=clean_thread or "decisions",
                msg_type="decision",
                body_md="\n".join(body_parts),
                data={"decision_id": ref, "action": "recorded"},
                pre_redacted=pre_redacted,
                now_iso=now_iso,
            )
            dec_rowid = decisions_store.insert_decision(
                conn,
                decision_id=dec_id,
                board_id=board["id"],
                number=num,
                title=clean_title,
                statement=clean_statement,
                rationale=clean_rationale,
                decided_by=clean_decided_by,
                decided_on=clean_date,
                recorded_by=ctx.principal_id,
                tags_json=json.dumps(clean_tags),
                source_message_id=msg["id"],
                now_iso=now_iso,
            )
            row = decisions_store.get_decision_by_ref(conn, board["id"], "id", dec_id)
            assert row is not None
            decisions_store.insert_decision_version(
                conn,
                decision_id=dec_id,
                version=1,
                snapshot_json=_snapshot_payload(row),
                changed_by=ctx.principal_id,
                changed_at=now_iso,
                message_id=msg["id"],
            )
            decisions_store.upsert_decision_fts(
                conn,
                decision_rowid=dec_rowid,
                decision_id=dec_id,
                board_id=board["id"],
                title=clean_title,
                statement=clean_statement,
                rationale=clean_rationale,
            )
            record_audit(
                conn,
                ctx=ctx,
                action="decision.record",
                ok=True,
                board_id=board["id"],
                target=ref,
            )
            return decision_row_to_dict(row)

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"decision.record:{board['slug']}",
            request_payload={
                "title": clean_title,
                "statement": clean_statement,
                "decided_by": clean_decided_by,
                "decided_on": clean_date,
                "rationale": clean_rationale,
                "tags": clean_tags,
                "thread_title": clean_thread,
            },
            status_code=201,
            fn=_do,
        )
        return res


def supersede_decision(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    old_ref: str,
    title: str,
    statement: str,
    decided_by: str,
    decided_on: str,
    reason: str,
    rationale: str | None = None,
    tags: list[str] | None = None,
    thread_title: str = "decisions",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    clean_title = title.strip()
    clean_statement = statement.strip()
    clean_decided_by = decided_by.strip()
    clean_reason = reason.strip()
    if not clean_title or not clean_statement or not clean_decided_by or not clean_reason:
        raise ValidationError("title, statement, decided_by, and reason are required")
    try:
        clean_date = validate_iso_date(decided_on.strip())
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    clean_tags = [t.strip().lower() for t in (tags or []) if t.strip()]
    raw_rationale = rationale.strip() if rationale and rationale.strip() else None

    clean_title, title_changed = services.redactor.scrub_text(clean_title)
    clean_statement, stmt_changed = services.redactor.scrub_text(clean_statement)
    clean_reason, reason_changed = services.redactor.scrub_text(clean_reason)
    clean_thread, thread_changed = services.redactor.scrub_text(thread_title or "decisions")
    clean_rationale: str | None = None
    rat_changed = False
    if raw_rationale is not None:
        clean_rationale, rat_changed = services.redactor.scrub_text(raw_rationale)
    pre_redacted = title_changed or stmt_changed or reason_changed or thread_changed or rat_changed

    with services.db.write_tx() as conn:
        board, eff_role = require_board_role(conn, ctx, board_slug, "agent")
        _require_decision_write_permission(board, eff_role)

        ref_kind, ref_val = parse_decision_ref(old_ref)
        old_row = decisions_store.get_decision_by_ref(conn, board["id"], ref_kind, ref_val)
        if old_row is None:
            raise NotFoundError(f"Decision '{old_ref}' not found on board '{board_slug}'")
        if old_row["status"] != "active":
            raise ConflictError(
                f"Decision D-{old_row['number']} has status '{old_row['status']}' and cannot be superseded",
                code="decision_not_active",
            )

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            new_num = boards_store.allocate_next_decision_num(conn, board["id"])
            old_label = f"D-{old_row['number']}"
            new_label = f"D-{new_num}"
            new_id = new_ulid()
            body_parts = [
                f"**{new_label}: {clean_title}** (supersedes {old_label})",
                "",
                clean_statement,
                "",
                f"_Supersession reason:_ {clean_reason}",
            ]
            if clean_rationale:
                body_parts.append(f"_Rationale:_ {clean_rationale}")
            body_parts.append(f"_Decided by {clean_decided_by} on {clean_date} (recorded by {ctx.handle})_")

            msg = post_message_in_tx(
                conn,
                services=services,
                ctx=ctx,
                board=board,
                thread_title=clean_thread or "decisions",
                msg_type="decision",
                body_md="\n".join(body_parts),
                data={
                    "decision_id": new_label,
                    "action": "superseded",
                    "superseded_id": old_label,
                    "superseded_by": new_label,
                    "reason": clean_reason,
                },
                pre_redacted=pre_redacted,
                now_iso=now_iso,
            )
            new_rowid = decisions_store.insert_decision(
                conn,
                decision_id=new_id,
                board_id=board["id"],
                number=new_num,
                title=clean_title,
                statement=clean_statement,
                rationale=clean_rationale,
                decided_by=clean_decided_by,
                decided_on=clean_date,
                recorded_by=ctx.principal_id,
                tags_json=json.dumps(clean_tags),
                source_message_id=msg["id"],
                now_iso=now_iso,
            )
            new_version = int(old_row["version"]) + 1
            decisions_store.mark_decision_superseded(
                conn,
                decision_id=old_row["id"],
                superseded_by_id=new_id,
                new_version=new_version,
                updated_at=now_iso,
            )

            new_row = decisions_store.get_decision_by_ref(conn, board["id"], "id", new_id)
            old_updated = decisions_store.get_decision_by_ref(conn, board["id"], "id", old_row["id"])
            assert new_row is not None and old_updated is not None

            decisions_store.insert_decision_version(
                conn,
                decision_id=new_id,
                version=1,
                snapshot_json=_snapshot_payload(new_row),
                changed_by=ctx.principal_id,
                changed_at=now_iso,
                message_id=msg["id"],
            )
            decisions_store.insert_decision_version(
                conn,
                decision_id=old_row["id"],
                version=new_version,
                snapshot_json=_snapshot_payload(old_updated),
                changed_by=ctx.principal_id,
                changed_at=now_iso,
                message_id=msg["id"],
            )
            decisions_store.upsert_decision_fts(
                conn,
                decision_rowid=new_rowid,
                decision_id=new_id,
                board_id=board["id"],
                title=clean_title,
                statement=clean_statement,
                rationale=clean_rationale,
            )
            record_audit(
                conn,
                ctx=ctx,
                action="decision.supersede",
                ok=True,
                board_id=board["id"],
                target=f"{old_label}->{new_label}",
                detail={"reason": clean_reason},
            )
            return decision_row_to_dict(new_row)

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"decision.supersede:{board['slug']}:{old_row['id']}",
            request_payload={
                "title": clean_title,
                "statement": clean_statement,
                "decided_by": clean_decided_by,
                "decided_on": clean_date,
                "reason": clean_reason,
                "rationale": clean_rationale,
                "tags": clean_tags,
            },
            status_code=201,
            fn=_do,
        )
        return res


def revoke_decision(
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
        raise ValidationError("Revocation reason is required")
    clean_reason, reason_changed = services.redactor.scrub_text(clean_reason)

    with services.db.write_tx() as conn:
        board, _ = require_board_role(conn, ctx, board_slug, "owner")
        ref_kind, ref_val = parse_decision_ref(ref)
        row = decisions_store.get_decision_by_ref(conn, board["id"], ref_kind, ref_val)
        if row is None:
            raise NotFoundError(f"Decision '{ref}' not found on board '{board_slug}'")
        if row["status"] != "active":
            raise ConflictError(
                f"Decision D-{row['number']} has status '{row['status']}' and cannot be revoked",
                code="decision_not_active",
            )

        def _do() -> dict[str, Any]:
            now_iso = utc_now_iso()
            label = f"D-{row['number']}"
            msg = post_message_in_tx(
                conn,
                services=services,
                ctx=ctx,
                board=board,
                thread_title="decisions",
                msg_type="decision",
                body_md=f"**{label} revoked:** {row['title']}\n\n_Reason:_ {clean_reason}",
                data={"decision_id": label, "action": "revoked", "reason": clean_reason},
                pre_redacted=reason_changed,
                now_iso=now_iso,
            )
            new_version = int(row["version"]) + 1
            decisions_store.mark_decision_revoked(
                conn,
                decision_id=row["id"],
                new_version=new_version,
                updated_at=now_iso,
            )
            updated = decisions_store.get_decision_by_ref(conn, board["id"], "id", row["id"])
            assert updated is not None
            decisions_store.insert_decision_version(
                conn,
                decision_id=row["id"],
                version=new_version,
                snapshot_json=_snapshot_payload(updated),
                changed_by=ctx.principal_id,
                changed_at=now_iso,
                message_id=msg["id"],
            )
            record_audit(
                conn,
                ctx=ctx,
                action="decision.revoke",
                ok=True,
                board_id=board["id"],
                target=label,
                detail={"reason": clean_reason},
            )
            return decision_row_to_dict(updated)

        res, _, _ = execute_idempotent(
            conn,
            ctx=ctx,
            key=idempotency_key,
            operation=f"decision.revoke:{board['slug']}:{row['id']}",
            request_payload={"reason": clean_reason},
            status_code=200,
            fn=_do,
        )
        return res


def list_decisions(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    status: str = "active",
) -> list[dict[str, Any]]:
    if status not in ("active", "superseded", "revoked", "all"):
        raise ValidationError("status must be active, superseded, revoked, or all")
    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    rows = decisions_store.list_decisions(conn, board["id"], status=status)
    return [decision_row_to_dict(r) for r in rows]


def get_decision(services: AppServices, ctx: AuthContext, *, board_slug: str, ref: str) -> dict[str, Any]:
    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    ref_kind, ref_val = parse_decision_ref(ref)
    row = decisions_store.get_decision_by_ref(conn, board["id"], ref_kind, ref_val)
    if row is None:
        raise NotFoundError(f"Decision '{ref}' not found on board '{board_slug}'")
    versions = decisions_store.list_decision_versions(conn, row["id"])
    return {
        "decision": decision_row_to_dict(row),
        "versions": [
            {
                "version": int(v["version"]),
                "snapshot": json.loads(v["snapshot_json"]),
                "changed_by_handle": v["changed_by_handle"],
                "changed_at": v["changed_at"],
                "message_id": v["message_id"],
            }
            for v in versions
        ],
    }


def export_decisions_md(services: AppServices, ctx: AuthContext, *, board_slug: str) -> str:
    conn = services.db.read_conn()
    board, _ = require_board_role(conn, ctx, board_slug, "readonly", allow_archived=True)
    all_rows = [decision_row_to_dict(r) for r in decisions_store.list_decisions(conn, board["id"], status="all")]
    active = [d for d in all_rows if d["status"] == "active"]
    historical = [d for d in all_rows if d["status"] != "active"]

    lines = [
        "<!-- generated from agent-board; do not edit -->",
        f"# Decisions — {board['name']} (`{board['slug']}`)",
        "",
        f"> {board['purpose']}",
        "",
        "## Active Decisions",
        "",
    ]
    if not active:
        lines.append("_No active decisions._")
        lines.append("")
    for d in active:
        tag_str = f" · tags: {', '.join(d['tags'])}" if d["tags"] else ""
        lines.append(
            f"### {d['ref']}: {d['title']} ({d['decided_on']}, "
            f"decided by {d['decided_by']}, recorded by {d['recorded_by_handle']}{tag_str})"
        )
        lines.append("")
        lines.append(d["statement"])
        if d["rationale"]:
            lines.append("")
            lines.append(f"*Rationale:* {d['rationale']}")
        lines.append("")

    if historical:
        lines.extend(["## Appendix: Superseded & Revoked Decisions", ""])
        for d in historical:
            extra = f" → superseded by {d['superseded_by_ref']}" if d["superseded_by_ref"] else ""
            lines.append(
                f"- **{d['ref']}** [{d['status']}{extra}] ({d['decided_on']}, {d['decided_by']}): "
                f"**{d['title']}** — {d['statement']}"
            )
        lines.append("")

    return "\n".join(lines)
