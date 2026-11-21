"""Configuration loading from environment variables and optional config.toml."""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agent_board.errors import ValidationError


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class Settings:
    """Runtime settings for Agent Board."""

    data_dir: Path = field(default_factory=lambda: Path("./data").resolve())
    bind: str = "127.0.0.1:8790"
    public_url: str | None = None
    secret_key: str = ""
    migrations_dir: Path = field(default_factory=lambda: _repo_root() / "migrations")
    schemas_dir: Path = field(default_factory=lambda: _repo_root() / "schemas")
    max_message_bytes: int = 65536
    max_attachment_bytes: int = 25 * 1024 * 1024
    max_mcp_attachment_bytes: int = 5 * 1024 * 1024
    max_board_attachments_bytes: int = 2 * 1024 * 1024 * 1024
    rate_limit_requests_per_min: int = 120
    rate_limit_writes_per_min: int = 30
    login_max_failures: int = 5
    login_lockout_seconds: int = 900
    backup_interval_seconds: int = 86400
    backup_retain_daily: int = 14
    backup_retain_weekly: int = 8
    nudge_poll_interval_s: float = 1.0
    nudge_timeout_s: float = 10.0
    redact_env_files: tuple[str, ...] = ()
    web_auto_login_handle: str | None = None
    web_trusted_ips: tuple[str, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "board.db"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def bind_host(self) -> str:
        host, _, _ = self.bind.rpartition(":")
        return host or "127.0.0.1"

    @property
    def bind_port(self) -> int:
        _, _, port = self.bind.rpartition(":")
        return int(port) if port.isdigit() else 8790

    @property
    def is_https_public(self) -> bool:
        return bool(self.public_url and self.public_url.startswith("https://"))


def _ensure_secret_key(data_dir: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("AGENT_BOARD_REQUIRE_SECRET_KEY") == "1":
        raise ValidationError(
            "AGENT_BOARD_SECRET_KEY is required when AGENT_BOARD_REQUIRE_SECRET_KEY=1",
            code="config_error",
        )
    key_file = data_dir / "secret.key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(48)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (generated + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return generated


def load_settings(data_dir: Path | None = None) -> Settings:
    """Load Settings from env vars and optional config.toml."""
    resolved_data = Path(data_dir or os.environ.get("AGENT_BOARD_DATA_DIR") or "./data").expanduser().resolve()
    resolved_data.mkdir(parents=True, exist_ok=True)

    file_cfg: dict[str, object] = {}
    config_path = Path(os.environ.get("AGENT_BOARD_CONFIG") or (resolved_data / "config.toml")).expanduser()
    if config_path.exists():
        with config_path.open("rb") as f:
            file_cfg = tomllib.load(f)

    def _get(env_name: str, cfg_key: str, default: object) -> object:
        if env_name in os.environ:
            return os.environ[env_name]
        return file_cfg.get(cfg_key, default)

    secret = str(_get("AGENT_BOARD_SECRET_KEY", "secret_key", "") or "")
    secret = _ensure_secret_key(resolved_data, secret or None)

    public_url_raw = _get("AGENT_BOARD_PUBLIC_URL", "public_url", None)
    public_url = str(public_url_raw).rstrip("/") if public_url_raw else None

    redact_raw = _get("AGENT_BOARD_REDACT_ENV_FILES", "redact_env_files", ())
    if isinstance(redact_raw, str):
        redact_env_files = tuple(p.strip() for p in redact_raw.split(",") if p.strip())
    elif isinstance(redact_raw, (list, tuple)):
        redact_env_files = tuple(str(p).strip() for p in redact_raw if str(p).strip())
    else:
        redact_env_files = ()

    auto_login_raw = _get("AGENT_BOARD_WEB_AUTO_LOGIN_HANDLE", "web_auto_login_handle", None)
    web_auto_login_handle = str(auto_login_raw).strip().lower() or None if auto_login_raw else None

    trusted_ips_raw = _get("AGENT_BOARD_WEB_TRUSTED_IPS", "web_trusted_ips", ())
    if isinstance(trusted_ips_raw, str):
        web_trusted_ips = tuple(ip.strip() for ip in trusted_ips_raw.split(",") if ip.strip())
    elif isinstance(trusted_ips_raw, (list, tuple)):
        web_trusted_ips = tuple(str(ip).strip() for ip in trusted_ips_raw if str(ip).strip())
    else:
        web_trusted_ips = ()

    return Settings(
        data_dir=resolved_data,
        bind=str(_get("AGENT_BOARD_BIND", "bind", "127.0.0.1:8790")),
        public_url=public_url,
        secret_key=secret,
        max_message_bytes=int(_get("AGENT_BOARD_MAX_MESSAGE_BYTES", "max_message_bytes", 65536)),
        max_attachment_bytes=int(_get("AGENT_BOARD_MAX_ATTACHMENT_BYTES", "max_attachment_bytes", 25 * 1024 * 1024)),
        max_mcp_attachment_bytes=int(
            _get(
                "AGENT_BOARD_MAX_MCP_ATTACHMENT_BYTES",
                "max_mcp_attachment_bytes",
                5 * 1024 * 1024,
            )
        ),
        max_board_attachments_bytes=int(
            _get(
                "AGENT_BOARD_MAX_BOARD_ATTACHMENTS_BYTES",
                "max_board_attachments_bytes",
                2 * 1024 * 1024 * 1024,
            )
        ),
        rate_limit_requests_per_min=int(_get("AGENT_BOARD_RATE_LIMIT_RPM", "rate_limit_requests_per_min", 120)),
        rate_limit_writes_per_min=int(_get("AGENT_BOARD_RATE_LIMIT_WPM", "rate_limit_writes_per_min", 30)),
        nudge_poll_interval_s=float(_get("AGENT_BOARD_NUDGE_POLL_S", "nudge_poll_interval_s", 1.0)),
        redact_env_files=redact_env_files,
        web_auto_login_handle=web_auto_login_handle,
        web_trusted_ips=web_trusted_ips,
    )
