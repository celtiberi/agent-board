"""Shared request authentication context and service container."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_board.config import Settings
from agent_board.crypto import SecretBox
from agent_board.secrets_scan import SecretRedactor
from agent_board.store.db import Database

ROLE_RANK: dict[str, int] = {
    "readonly": 1,
    "agent": 2,
    "owner": 3,
}


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Resolved caller identity and effective scope for one request."""

    principal_id: str
    handle: str
    kind: str
    display_name: str
    is_server_owner: bool
    auth_kind: str  # "token" | "session" | "trusted_ip" | "system"
    token_id: str | None = None
    token_prefix: str | None = None
    board_scope: tuple[str, ...] = ("*",)
    max_role: str = "owner"
    csrf_token: str | None = None
    ip: str | None = None

    @property
    def has_server_owner_privilege(self) -> bool:
        """True only when caller is system or a server owner with unrestricted owner scope."""
        return self.auth_kind == "system" or (
            self.is_server_owner and self.max_role == "owner" and "*" in self.board_scope
        )

    def allows_board(self, slug: str) -> bool:
        return "*" in self.board_scope or slug in self.board_scope

    def effective_role(self, membership_role: str | None) -> str | None:
        if self.auth_kind == "system":
            return "owner"
        if membership_role is None:
            return None
        eff_rank = min(ROLE_RANK[membership_role], ROLE_RANK[self.max_role])
        for role, rank in ROLE_RANK.items():
            if rank == eff_rank:
                return role
        return None


def system_context() -> AuthContext:
    """Return the internal system principal context."""
    return AuthContext(
        principal_id="00000000000000000000000000",
        handle="system",
        kind="system",
        display_name="System",
        is_server_owner=False,
        auth_kind="system",
        board_scope=("*",),
        max_role="owner",
    )


def load_message_data_schemas(schemas_dir: Path) -> dict[str, dict[str, Any]]:
    """Load JSON Schemas for each §4 message type."""
    msg_dir = schemas_dir / "message_data"
    loaded: dict[str, dict[str, Any]] = {}
    for path in sorted(msg_dir.glob("*.json")):
        loaded[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


@dataclass
class AppServices:
    """Application dependency container wired into HTTP, MCP, Web, Worker, and CLI."""

    settings: Settings
    db: Database
    crypto: SecretBox
    message_schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    redactor: SecretRedactor = field(default_factory=SecretRedactor)
    rate_limiter: Any = None
    nudge_worker: Any = None
    backup_task: Any = None

    @classmethod
    def create(cls, settings: Settings) -> AppServices:
        from agent_board.services.auth import RateLimiter

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.attachments_dir.mkdir(parents=True, exist_ok=True)
        settings.backups_dir.mkdir(parents=True, exist_ok=True)

        db = Database(settings.db_path)
        db.apply_migrations(settings.migrations_dir)
        crypto = SecretBox(settings.secret_key)
        schemas = load_message_data_schemas(settings.schemas_dir)
        redactor = SecretRedactor.from_environment(
            extra_secrets=[settings.secret_key],
            env_files=settings.redact_env_files,
        )
        limiter = RateLimiter(
            requests_per_min=settings.rate_limit_requests_per_min,
            writes_per_min=settings.rate_limit_writes_per_min,
        )
        return cls(
            settings=settings,
            db=db,
            crypto=crypto,
            message_schemas=schemas,
            redactor=redactor,
            rate_limiter=limiter,
        )

    def close(self) -> None:
        self.db.close()
