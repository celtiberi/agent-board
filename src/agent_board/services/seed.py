"""Idempotent server initialization and declarative board/workspace seeding."""

from __future__ import annotations

from typing import Any

from agent_board.errors import ValidationError
from agent_board.ids import new_ulid, utc_now_iso
from agent_board.services import boards as boards_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import issues as issues_svc
from agent_board.services.auth import create_principal, set_human_password, set_membership
from agent_board.services.context import AppServices, AuthContext, system_context
from agent_board.store import boards as boards_store
from agent_board.store import decisions as decisions_store
from agent_board.store import issues as issues_store
from agent_board.store import principals as principals_store

_DEMO_SPEC: dict[str, Any] = {
    "principals": [
        {"handle": "qa-agent", "kind": "agent", "display_name": "QA Agent"},
        {"handle": "dev-agent", "kind": "agent", "display_name": "Dev Agent"},
    ],
    "boards": [
        {
            "slug": "example-app",
            "name": "Example App",
            "purpose": "Sample coordination board for human operators, QA agents, and dev agents.",
            "members": [
                {"handle": "qa-agent", "role": "agent"},
                {"handle": "dev-agent", "role": "agent"},
            ],
            "threads": ["qa", "architecture"],
            "decisions": [
                {
                    "title": "Store timestamps in UTC",
                    "statement": "Persist all API and database timestamps in UTC ISO-8601 format.",
                    "decided_by": "owner",
                    "decided_on": "2026-11-16",
                    "rationale": "Avoids daylight saving ambiguity across distributed clients.",
                    "tags": ["architecture", "time"],
                },
                {
                    "title": "One canonical source per fact",
                    "statement": "Calculate each domain metric in one shared service; UI and API render that result.",
                    "decided_by": "owner",
                    "decided_on": "2026-11-16",
                    "rationale": "Prevents drift between API payloads and rendered views.",
                    "tags": ["architecture", "quality"],
                },
            ],
            "issues": [
                {
                    "title": "Deduplicate webhook events on ingest",
                    "body_md": "Repeated delivery retries should be deduplicated once at the ingest boundary.",
                    "severity": "high",
                    "assignee": "dev-agent",
                    "tags": ["ingest", "reliability"],
                },
            ],
        }
    ],
}


def _owner_context(conn: Any) -> AuthContext:
    row = principals_store.get_first_server_owner(conn)
    if row is None:
        return system_context()
    return AuthContext(
        principal_id=row["id"],
        handle=row["handle"],
        kind=row["kind"],
        display_name=row["display_name"],
        is_server_owner=bool(row["is_server_owner"]),
        auth_kind="system",
        board_scope=("*",),
        max_role="owner",
    )


def ensure_owner_and_ops(
    services: AppServices,
    *,
    owner_handle: str = "owner",
    owner_password: str | None = None,
) -> dict[str, Any]:
    """Ensure a server owner principal and 'ops' board exist."""
    created_owner = False
    clean_handle = owner_handle.strip().lower() or "owner"
    effective_handle = clean_handle
    with services.db.write_tx() as conn:
        owner_row = principals_store.get_first_server_owner(conn)
        if owner_row is None:
            pid = new_ulid()
            now_iso = utc_now_iso()
            principals_store.insert_principal(
                conn,
                principal_id=pid,
                handle=clean_handle,
                kind="human",
                display_name=clean_handle.capitalize(),
                is_server_owner=True,
                created_at=now_iso,
            )
            created_owner = True
        else:
            effective_handle = owner_row["handle"]
        owner_ctx = _owner_context(conn)
        if boards_store.get_board_by_slug(conn, "ops") is None:
            boards_svc.create_board_in_tx(
                conn,
                ctx=owner_ctx,
                slug="ops",
                name="Server Ops",
                purpose="Server-level operational alerts, backup notifications, and subscription health.",
            )

    if owner_password:
        set_human_password(services, handle=effective_handle, password=owner_password)
    return {"owner_created": created_owner, "owner_handle": effective_handle}


def seed_from_spec(services: AppServices, spec: dict[str, Any]) -> dict[str, Any]:
    """Idempotently seed principals, boards, memberships, threads, decisions, and issues from a spec dict."""
    if not isinstance(spec, dict):
        raise ValidationError("Seed specification must be a JSON object")

    owner_info = ensure_owner_and_ops(services)
    conn = services.db.read_conn()
    owner_ctx = _owner_context(conn)

    seeded_principals: list[str] = [owner_info["owner_handle"]]
    for p in spec.get("principals", []):
        handle = str(p["handle"]).strip().lower()
        if principals_store.get_principal_by_handle(services.db.read_conn(), handle) is None:
            create_principal(
                services,
                owner_ctx,
                handle=handle,
                kind=str(p.get("kind", "agent")),
                display_name=str(p.get("display_name") or handle),
                is_server_owner=False,
            )
        if handle not in seeded_principals:
            seeded_principals.append(handle)

    seeded_boards: list[str] = ["ops"]
    for b in spec.get("boards", []):
        slug = str(b["slug"]).strip().lower()
        name = str(b.get("name") or slug)
        purpose = str(b.get("purpose") or f"Coordination board for {name}.")
        members = [(str(m["handle"]).strip().lower(), str(m.get("role", "agent"))) for m in b.get("members", [])]
        with services.db.write_tx() as wconn:
            board_row = boards_store.get_board_by_slug(wconn, slug)
            if board_row is None:
                board_row = boards_svc.create_board_in_tx(
                    wconn,
                    ctx=owner_ctx,
                    slug=slug,
                    name=name,
                    purpose=purpose,
                    settings=b.get("settings"),
                    extra_members=members,
                )
            now_iso = utc_now_iso()
            for title in b.get("threads", []):
                clean_title = str(title).strip()
                if clean_title and boards_store.get_thread_by_title(wconn, board_row["id"], clean_title) is None:
                    boards_store.insert_thread(
                        wconn,
                        thread_id=new_ulid(),
                        board_id=board_row["id"],
                        title=clean_title,
                        created_by=owner_ctx.principal_id,
                        created_at=now_iso,
                    )

        for handle, role in members:
            set_membership(services, owner_ctx, board_slug=slug, principal_handle=handle, role=role)

        board_row = boards_store.get_board_by_slug(services.db.read_conn(), slug)
        assert board_row is not None
        if not decisions_store.list_decisions(services.db.read_conn(), board_row["id"], status="all"):
            for dec in b.get("decisions", []):
                decisions_svc.record_decision(
                    services,
                    owner_ctx,
                    board_slug=slug,
                    title=str(dec["title"]),
                    statement=str(dec["statement"]),
                    decided_by=str(dec.get("decided_by") or owner_info["owner_handle"]),
                    decided_on=str(dec["decided_on"]),
                    rationale=dec.get("rationale"),
                    tags=list(dec.get("tags", [])),
                )

        if not issues_store.list_issues(services.db.read_conn(), board_row["id"], status="all"):
            for iss in b.get("issues", []):
                issues_svc.open_issue(
                    services,
                    owner_ctx,
                    board_slug=slug,
                    title=str(iss["title"]),
                    body_md=str(iss["body_md"]),
                    severity=str(iss.get("severity", "normal")),
                    assignee=iss.get("assignee"),
                    tags=list(iss.get("tags", [])),
                )

        if slug not in seeded_boards:
            seeded_boards.append(slug)

    return {"boards": seeded_boards, "principals": seeded_principals}


def seed_demo_workspace(services: AppServices) -> dict[str, Any]:
    """Seed a generic example workspace for demos and restore drills."""
    return seed_from_spec(services, _DEMO_SPEC)
