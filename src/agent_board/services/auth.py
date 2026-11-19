"""Authentication, role resolution, rate limiting, and audit helpers."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Any

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from agent_board.errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ValidationError,
)
from agent_board.ids import (
    RESERVED_HANDLES,
    dt_to_iso,
    generate_api_token,
    hash_token,
    is_valid_handle,
    iso_to_dt,
    new_ulid,
    normalize_principal_tags,
    utc_now,
    utc_now_iso,
)
from agent_board.services.context import ROLE_RANK, AppServices, AuthContext
from agent_board.store import audit as audit_store
from agent_board.store import boards as boards_store
from agent_board.store import principals as principals_store

_PH = PasswordHasher()


class RateLimiter:
    """In-memory per-identity sliding 60-second rate limiter."""

    def __init__(self, *, requests_per_min: int = 120, writes_per_min: int = 30) -> None:
        self.requests_per_min = requests_per_min
        self.writes_per_min = writes_per_min
        self._lock = threading.Lock()
        self._all_hits: dict[str, deque[float]] = defaultdict(deque)
        self._write_hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, identity_key: str, *, is_write: bool) -> None:
        if self.requests_per_min <= 0 and self.writes_per_min <= 0:
            return
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            all_q = self._all_hits[identity_key]
            while all_q and all_q[0] <= cutoff:
                all_q.popleft()
            if self.requests_per_min > 0 and len(all_q) >= self.requests_per_min:
                retry_after = max(1, int(60.0 - (now - all_q[0])))
                raise RateLimitError("Request rate limit exceeded", retry_after=retry_after)

            if is_write and self.writes_per_min > 0:
                write_q = self._write_hits[identity_key]
                while write_q and write_q[0] <= cutoff:
                    write_q.popleft()
                if len(write_q) >= self.writes_per_min:
                    retry_after = max(1, int(60.0 - (now - write_q[0])))
                    raise RateLimitError("Write rate limit exceeded", retry_after=retry_after)
                write_q.append(now)

            all_q.append(now)


def record_audit(
    conn: sqlite3.Connection,
    *,
    ctx: AuthContext | None,
    action: str,
    ok: bool,
    board_id: str | None = None,
    target: str | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    audit_store.insert_audit_log(
        conn,
        audit_id=new_ulid(),
        at=utc_now_iso(),
        principal_id=ctx.principal_id if ctx else None,
        token_prefix=ctx.token_prefix if ctx else None,
        action=action,
        board_id=board_id,
        target=target,
        ip=ip or (ctx.ip if ctx else None),
        ok=ok,
        detail_json=json.dumps(detail or {}, separators=(",", ":")),
    )


def _record_auth_failure(
    services: AppServices, *, action: str, ip: str | None, reason: str, target: str | None = None
) -> None:
    with services.db.write_tx() as conn:
        record_audit(
            conn,
            ctx=None,
            action=action,
            ok=False,
            target=target,
            ip=ip,
            detail={"reason": reason},
        )


def authenticate_bearer(services: AppServices, raw_token: str | None, *, ip: str | None = None) -> AuthContext:
    if not raw_token or not raw_token.startswith("ab_"):
        _record_auth_failure(services, action="auth.token", ip=ip, reason="missing_or_malformed")
        raise AuthError("Invalid or missing bearer token")

    token_hash = hash_token(raw_token)
    row = principals_store.get_token_by_hash(services.db.read_conn(), token_hash)
    if row is None:
        _record_auth_failure(services, action="auth.token", ip=ip, reason="unknown_token", target=raw_token[:11])
        raise AuthError("Invalid bearer token")

    now = utc_now()
    if row["revoked_at"] is not None:
        _record_auth_failure(services, action="auth.token", ip=ip, reason="revoked", target=row["prefix"])
        raise AuthError("Token has been revoked", code="token_revoked")
    if row["principal_disabled_at"] is not None:
        _record_auth_failure(services, action="auth.token", ip=ip, reason="principal_disabled", target=row["prefix"])
        raise AuthError("Principal is disabled", code="principal_disabled")
    if row["expires_at"] is not None and iso_to_dt(row["expires_at"]) <= now:
        _record_auth_failure(services, action="auth.token", ip=ip, reason="expired", target=row["prefix"])
        raise AuthError("Token has expired", code="token_expired")

    last_used = row["last_used_at"]
    if last_used is None or (now - iso_to_dt(last_used)).total_seconds() > 300:
        with services.db.write_tx() as conn:
            principals_store.touch_token_last_used(conn, row["id"], dt_to_iso(now))

    scope = tuple(json.loads(row["board_scope"]))
    return AuthContext(
        principal_id=row["principal_id"],
        handle=row["principal_handle"],
        kind=row["principal_kind"],
        display_name=row["principal_display_name"],
        is_server_owner=bool(row["is_server_owner"]),
        auth_kind="token",
        token_id=row["id"],
        token_prefix=row["prefix"],
        board_scope=scope,
        max_role=row["max_role"],
        ip=ip,
    )


def authenticate_session(
    services: AppServices, session_token: str | None, *, ip: str | None = None
) -> AuthContext | None:
    if not session_token:
        return None
    row = principals_store.get_web_session(services.db.read_conn(), hash_token(session_token))
    if row is None:
        return None
    if row["revoked_at"] is not None or row["principal_disabled_at"] is not None:
        return None
    if iso_to_dt(row["expires_at"]) <= utc_now():
        return None
    return AuthContext(
        principal_id=row["principal_id"],
        handle=row["principal_handle"],
        kind=row["principal_kind"],
        display_name=row["principal_display_name"],
        is_server_owner=bool(row["is_server_owner"]),
        auth_kind="session",
        board_scope=("*",),
        max_role="owner",
        csrf_token=row["csrf_token"],
        ip=ip,
    )


def login_human(
    services: AppServices,
    *,
    handle: str,
    password: str,
    totp_code: str | None = None,
    ip: str | None = None,
) -> tuple[str, AuthContext]:
    now = utc_now()
    now_iso = dt_to_iso(now)
    error_to_raise: Exception | None = None
    result: tuple[str, AuthContext] | None = None

    with services.db.write_tx() as conn:
        principal = principals_store.get_principal_by_handle(conn, handle.strip().lower())
        if principal is None or principal["kind"] != "human" or principal["disabled_at"] is not None:
            record_audit(
                conn,
                ctx=None,
                action="auth.login",
                ok=False,
                target=handle,
                ip=ip,
                detail={"reason": "unknown_or_disabled"},
            )
            error_to_raise = AuthError("Invalid username or password")
        else:
            auth_row = principals_store.get_human_auth(conn, principal["id"])
            if auth_row is None:
                record_audit(
                    conn,
                    ctx=None,
                    action="auth.login",
                    ok=False,
                    target=handle,
                    ip=ip,
                    detail={"reason": "no_password_set"},
                )
                error_to_raise = AuthError("Invalid username or password")
            else:
                locked_until = auth_row["locked_until"]
                if locked_until and iso_to_dt(locked_until) > now:
                    retry_after = max(1, int((iso_to_dt(locked_until) - now).total_seconds()))
                    record_audit(
                        conn,
                        ctx=None,
                        action="auth.login",
                        ok=False,
                        target=handle,
                        ip=ip,
                        detail={"reason": "locked"},
                    )
                    error_to_raise = RateLimitError(
                        "Too many failed login attempts; try again later", retry_after=retry_after
                    )
                else:
                    password_ok = False
                    try:
                        password_ok = _PH.verify(auth_row["password_hash"], password)
                    except VerifyMismatchError:
                        password_ok = False

                    totp_ok = True
                    if password_ok and auth_row["totp_secret_enc"]:
                        secret = services.crypto.decrypt(auth_row["totp_secret_enc"])
                        totp = pyotp.TOTP(secret)
                        totp_ok = bool(totp_code and totp.verify(totp_code.strip(), valid_window=1))

                    if not password_ok or not totp_ok:
                        failures = int(auth_row["failed_attempts"]) + 1
                        new_locked: str | None = None
                        if failures >= services.settings.login_max_failures:
                            new_locked = dt_to_iso(now + timedelta(seconds=services.settings.login_lockout_seconds))
                        principals_store.update_human_auth_failure(
                            conn,
                            principal_id=principal["id"],
                            failed_attempts=failures,
                            locked_until=new_locked,
                            updated_at=now_iso,
                        )
                        reason = "bad_totp" if password_ok else "bad_password"
                        record_audit(
                            conn,
                            ctx=None,
                            action="auth.login",
                            ok=False,
                            target=handle,
                            ip=ip,
                            detail={"reason": reason, "failures": failures},
                        )
                        if not password_ok:
                            error_to_raise = AuthError("Invalid username or password")
                        else:
                            error_to_raise = AuthError("Invalid TOTP verification code", code="invalid_totp")
                    else:
                        principals_store.reset_human_auth_failures(conn, principal["id"], now_iso)
                        session_secret = secrets.token_urlsafe(32)
                        csrf_token = secrets.token_urlsafe(24)
                        expires_at = dt_to_iso(now + timedelta(days=14))
                        principals_store.insert_web_session(
                            conn,
                            id_hash=hash_token(session_secret),
                            principal_id=principal["id"],
                            csrf_token=csrf_token,
                            created_at=now_iso,
                            expires_at=expires_at,
                        )
                        ctx = AuthContext(
                            principal_id=principal["id"],
                            handle=principal["handle"],
                            kind=principal["kind"],
                            display_name=principal["display_name"],
                            is_server_owner=bool(principal["is_server_owner"]),
                            auth_kind="session",
                            board_scope=("*",),
                            max_role="owner",
                            csrf_token=csrf_token,
                            ip=ip,
                        )
                        record_audit(
                            conn,
                            ctx=ctx,
                            action="auth.login",
                            ok=True,
                            target=principal["handle"],
                            ip=ip,
                        )
                        result = (session_secret, ctx)

    if error_to_raise is not None:
        raise error_to_raise
    assert result is not None
    return result


def logout_session(services: AppServices, session_token: str | None) -> None:
    if not session_token:
        return
    with services.db.write_tx() as conn:
        principals_store.revoke_web_session(conn, hash_token(session_token), utc_now_iso())


def require_server_owner(ctx: AuthContext) -> None:
    if not ctx.is_server_owner and ctx.auth_kind != "system":
        raise PermissionDeniedError("Server owner privileges required", code="owner_required")


def require_board_role(
    conn: sqlite3.Connection,
    ctx: AuthContext,
    board_slug: str,
    min_role: str,
    *,
    allow_archived: bool = False,
) -> tuple[sqlite3.Row, str]:
    board = boards_store.get_board_by_slug(conn, board_slug)
    if board is None:
        raise NotFoundError(f"Board '{board_slug}' not found", code="board_not_found")
    if board["archived_at"] is not None and not allow_archived:
        raise ValidationError(f"Board '{board_slug}' is archived", code="board_archived")

    if ctx.auth_kind == "system":
        return board, "owner"

    if not ctx.allows_board(board["slug"]):
        raise PermissionDeniedError(
            f"Token scope does not include board '{board_slug}'",
            code="board_out_of_scope",
        )

    membership = boards_store.get_membership(conn, board["id"], ctx.principal_id)
    membership_role = membership["role"] if membership is not None else None
    if membership_role is None and ctx.is_server_owner:
        membership_role = "owner"

    eff = ctx.effective_role(membership_role)
    if eff is None or ROLE_RANK[eff] < ROLE_RANK[min_role]:
        raise PermissionDeniedError(
            f"Role '{min_role}' required on board '{board_slug}'",
            code="insufficient_role",
        )
    return board, eff


def _parse_tags(tags: list[str] | None) -> list[str]:
    try:
        return normalize_principal_tags(tags)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def create_principal(
    services: AppServices,
    ctx: AuthContext,
    *,
    handle: str,
    kind: str,
    display_name: str,
    is_server_owner: bool = False,
    password: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    require_server_owner(ctx)
    clean_handle = handle.strip().lower()
    if not is_valid_handle(clean_handle) or clean_handle in RESERVED_HANDLES:
        raise ValidationError(f"Invalid handle '{handle}'")
    if kind not in ("human", "agent"):
        raise ValidationError("kind must be 'human' or 'agent'")
    if not display_name.strip():
        raise ValidationError("display_name is required")
    clean_tags = _parse_tags(tags)

    now_iso = utc_now_iso()
    pid = new_ulid()
    with services.db.write_tx() as conn:
        if principals_store.get_principal_by_handle(conn, clean_handle) is not None:
            raise ConflictError(f"Principal '{clean_handle}' already exists", code="duplicate_handle")
        principals_store.insert_principal(
            conn,
            principal_id=pid,
            handle=clean_handle,
            kind=kind,
            display_name=display_name.strip(),
            is_server_owner=is_server_owner,
            created_at=now_iso,
            tags_json=json.dumps(clean_tags),
        )
        if password is not None:
            if kind != "human":
                raise ValidationError("Only human principals can have passwords")
            principals_store.upsert_human_auth(
                conn,
                principal_id=pid,
                password_hash=_PH.hash(password),
                totp_secret_enc=None,
                updated_at=now_iso,
            )
        record_audit(
            conn,
            ctx=ctx,
            action="principal.create",
            ok=True,
            target=clean_handle,
            detail={"kind": kind, "is_server_owner": is_server_owner, "tags": clean_tags},
        )
        row = principals_store.get_principal_by_id(conn, pid)
        return _principal_to_dict(row)


def set_human_password(
    services: AppServices,
    *,
    handle: str,
    password: str,
    totp_secret: str | None = None,
) -> None:
    if len(password) < 6:
        raise ValidationError("Password must be at least 6 characters")
    now_iso = utc_now_iso()
    with services.db.write_tx() as conn:
        principal = principals_store.get_principal_by_handle(conn, handle.strip().lower())
        if principal is None or principal["kind"] != "human":
            raise NotFoundError(f"Human principal '{handle}' not found")
        totp_enc = services.crypto.encrypt(totp_secret) if totp_secret else None
        principals_store.upsert_human_auth(
            conn,
            principal_id=principal["id"],
            password_hash=_PH.hash(password),
            totp_secret_enc=totp_enc,
            updated_at=now_iso,
        )


def set_principal_disabled(services: AppServices, ctx: AuthContext, *, handle: str, disabled: bool) -> dict[str, Any]:
    require_server_owner(ctx)
    clean_handle = handle.strip().lower()
    if clean_handle == "system":
        raise ValidationError("Cannot modify system principal")
    with services.db.write_tx() as conn:
        row = principals_store.get_principal_by_handle(conn, clean_handle)
        if row is None:
            raise NotFoundError(f"Principal '{clean_handle}' not found")
        disabled_at = utc_now_iso() if disabled else None
        principals_store.update_principal_disabled(conn, row["id"], disabled_at)
        record_audit(
            conn,
            ctx=ctx,
            action="principal.disable" if disabled else "principal.enable",
            ok=True,
            target=clean_handle,
        )
        return _principal_to_dict(principals_store.get_principal_by_id(conn, row["id"]))


def set_principal_tags(
    services: AppServices,
    ctx: AuthContext,
    *,
    handle: str,
    tags: list[str],
) -> dict[str, Any]:
    """Replace the capability/group tags assigned to a principal."""
    require_server_owner(ctx)
    clean_handle = handle.strip().lower()
    if clean_handle == "system":
        raise ValidationError("Cannot modify system principal")
    clean_tags = _parse_tags(tags)
    with services.db.write_tx() as conn:
        row = principals_store.get_principal_by_handle(conn, clean_handle)
        if row is None:
            raise NotFoundError(f"Principal '{clean_handle}' not found")
        principals_store.update_principal_tags(conn, row["id"], json.dumps(clean_tags))
        record_audit(
            conn,
            ctx=ctx,
            action="principal.tags",
            ok=True,
            target=clean_handle,
            detail={"tags": clean_tags},
        )
        updated = principals_store.get_principal_by_id(conn, row["id"])
        assert updated is not None
        return _principal_to_dict(updated)


def rename_principal(
    services: AppServices,
    ctx: AuthContext,
    *,
    handle: str,
    new_handle: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Rename a principal's @handle and/or display_name while preserving ID, tokens, and history."""
    require_server_owner(ctx)
    old_handle = handle.strip().lower()
    if not old_handle or old_handle == "system":
        raise ValidationError("Cannot modify system principal")
    if new_handle is None and display_name is None:
        raise ValidationError("Provide new_handle or display_name")

    target_handle = new_handle.strip().lower() if new_handle is not None else old_handle
    if not is_valid_handle(target_handle) or target_handle in RESERVED_HANDLES:
        raise ValidationError(f"Invalid handle '{new_handle}'")
    if display_name is not None and not display_name.strip():
        raise ValidationError("display_name must not be empty")

    with services.db.write_tx() as conn:
        row = principals_store.get_principal_by_handle(conn, old_handle)
        if row is None:
            raise NotFoundError(f"Principal '{old_handle}' not found")
        if target_handle != old_handle and principals_store.get_principal_by_handle(conn, target_handle) is not None:
            raise ConflictError(f"Principal '{target_handle}' already exists", code="duplicate_handle")
        target_display = display_name.strip() if display_name is not None else str(row["display_name"])
        principals_store.update_principal_identity(
            conn,
            row["id"],
            handle=target_handle,
            display_name=target_display,
        )
        record_audit(
            conn,
            ctx=ctx,
            action="principal.rename",
            ok=True,
            target=target_handle,
            detail={"old_handle": old_handle, "new_handle": target_handle, "display_name": target_display},
        )
        updated = principals_store.get_principal_by_id(conn, row["id"])
        assert updated is not None
        return _principal_to_dict(updated)


def list_principals(services: AppServices, ctx: AuthContext) -> list[dict[str, Any]]:
    require_server_owner(ctx)
    rows = principals_store.list_principals(services.db.read_conn())
    return [_principal_to_dict(r) for r in rows]


def create_token(
    services: AppServices,
    ctx: AuthContext,
    *,
    principal_handle: str,
    name: str,
    board_scope: list[str],
    max_role: str = "agent",
    expires_at: str | None = None,
) -> dict[str, Any]:
    require_server_owner(ctx)
    if max_role not in ROLE_RANK:
        raise ValidationError("max_role must be one of owner, agent, readonly")
    if not board_scope:
        raise ValidationError("board_scope must not be empty")
    clean_scope = [s.strip() for s in board_scope if s.strip()]
    if not clean_scope:
        raise ValidationError("board_scope must not be empty")
    if expires_at is not None:
        iso_to_dt(expires_at)

    plaintext, prefix, token_hash = generate_api_token()
    now_iso = utc_now_iso()
    token_id = new_ulid()
    with services.db.write_tx() as conn:
        principal = principals_store.get_principal_by_handle(conn, principal_handle.strip().lower())
        if principal is None or principal["kind"] == "system":
            raise NotFoundError(f"Principal '{principal_handle}' not found")
        principals_store.insert_token(
            conn,
            token_id=token_id,
            principal_id=principal["id"],
            name=name.strip() or f"{principal['handle']}-token",
            token_hash=token_hash,
            prefix=prefix,
            board_scope_json=json.dumps(clean_scope),
            max_role=max_role,
            created_at=now_iso,
            expires_at=expires_at,
        )
        record_audit(
            conn,
            ctx=ctx,
            action="token.create",
            ok=True,
            target=prefix,
            detail={"principal": principal["handle"], "scope": clean_scope, "max_role": max_role},
        )
    return {
        "id": token_id,
        "principal_handle": principal["handle"],
        "name": name.strip() or f"{principal['handle']}-token",
        "prefix": prefix,
        "token": plaintext,
        "board_scope": clean_scope,
        "max_role": max_role,
        "created_at": now_iso,
        "expires_at": expires_at,
    }


def list_tokens(services: AppServices, ctx: AuthContext) -> list[dict[str, Any]]:
    require_server_owner(ctx)
    rows = principals_store.list_tokens(services.db.read_conn())
    return [
        {
            "id": r["id"],
            "principal_id": r["principal_id"],
            "principal_handle": r["principal_handle"],
            "name": r["name"],
            "prefix": r["prefix"],
            "board_scope": json.loads(r["board_scope"]),
            "max_role": r["max_role"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "last_used_at": r["last_used_at"],
            "revoked_at": r["revoked_at"],
        }
        for r in rows
    ]


def revoke_token(services: AppServices, ctx: AuthContext, *, token_id: str) -> dict[str, Any]:
    require_server_owner(ctx)
    now_iso = utc_now_iso()
    with services.db.write_tx() as conn:
        changed = principals_store.revoke_token(conn, token_id, now_iso)
        if changed == 0:
            raise NotFoundError(f"Active token '{token_id}' not found")
        record_audit(conn, ctx=ctx, action="token.revoke", ok=True, target=token_id)
    return {"id": token_id, "revoked_at": now_iso}


def set_membership(
    services: AppServices,
    ctx: AuthContext,
    *,
    board_slug: str,
    principal_handle: str,
    role: str,
) -> dict[str, Any]:
    if role not in ROLE_RANK:
        raise ValidationError("role must be owner, agent, or readonly")
    now_iso = utc_now_iso()
    with services.db.write_tx() as conn:
        if ctx.is_server_owner:
            board = boards_store.get_board_by_slug(conn, board_slug)
            if board is None:
                raise NotFoundError(f"Board '{board_slug}' not found")
        else:
            board, _ = require_board_role(conn, ctx, board_slug, "owner")
        principal = principals_store.get_principal_by_handle(conn, principal_handle.strip().lower())
        if principal is None or principal["kind"] == "system":
            raise NotFoundError(f"Principal '{principal_handle}' not found")
        boards_store.upsert_membership(
            conn,
            board_id=board["id"],
            principal_id=principal["id"],
            role=role,
            created_at=now_iso,
        )
        if ctx.is_server_owner and principal["kind"] == "agent" and not bool(principal["is_server_owner"]):
            principals_store.sync_agent_token_board_scopes(
                conn, principal_id=principal["id"], add_slug=board["slug"]
            )
        record_audit(
            conn,
            ctx=ctx,
            action="membership.set",
            ok=True,
            board_id=board["id"],
            target=principal["handle"],
            detail={"role": role},
        )
    return {
        "board_slug": board["slug"],
        "principal_handle": principal["handle"],
        "role": role,
        "created_at": now_iso,
    }


def remove_membership(
    services: AppServices, ctx: AuthContext, *, board_slug: str, principal_handle: str
) -> dict[str, Any]:
    with services.db.write_tx() as conn:
        if ctx.is_server_owner:
            board = boards_store.get_board_by_slug(conn, board_slug)
            if board is None:
                raise NotFoundError(f"Board '{board_slug}' not found")
        else:
            board, _ = require_board_role(conn, ctx, board_slug, "owner")
        principal = principals_store.get_principal_by_handle(conn, principal_handle.strip().lower())
        if principal is None:
            raise NotFoundError(f"Principal '{principal_handle}' not found")
        deleted = boards_store.delete_membership(conn, board["id"], principal["id"])
        if deleted == 0:
            raise NotFoundError("Membership not found")
        if principal["kind"] == "agent" and not bool(principal["is_server_owner"]):
            principals_store.sync_agent_token_board_scopes(
                conn, principal_id=principal["id"], remove_slug=board["slug"]
            )
        record_audit(
            conn,
            ctx=ctx,
            action="membership.remove",
            ok=True,
            board_id=board["id"],
            target=principal["handle"],
        )
    return {"board_slug": board_slug, "principal_handle": principal["handle"], "removed": True}


def list_audit_logs(services: AppServices, ctx: AuthContext, *, limit: int = 200) -> list[dict[str, Any]]:
    require_server_owner(ctx)
    rows = audit_store.list_audit_logs(services.db.read_conn(), limit=min(max(limit, 1), 1000))
    return [
        {
            "id": r["id"],
            "at": r["at"],
            "principal_handle": r["principal_handle"],
            "token_prefix": r["token_prefix"],
            "action": r["action"],
            "board_slug": r["board_slug"],
            "target": r["target"],
            "ip": r["ip"],
            "ok": bool(r["ok"]),
            "detail": json.loads(r["detail_json"]),
        }
        for r in rows
    ]


def _principal_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    tags_raw = row["tags"] if "tags" in row.keys() and row["tags"] else "[]"
    return {
        "id": row["id"],
        "handle": row["handle"],
        "kind": row["kind"],
        "display_name": row["display_name"],
        "is_server_owner": bool(row["is_server_owner"]),
        "tags": json.loads(tags_raw),
        "created_at": row["created_at"],
        "disabled_at": row["disabled_at"],
    }
