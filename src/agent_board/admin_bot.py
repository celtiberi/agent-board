"""Deterministic allowlisted board-admin daemon with optional local-LLM intent fallback."""

from __future__ import annotations

import hmac
import importlib.resources
import json
import logging
import queue
import re
import shlex
import signal
import sys
import threading
import tomllib
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from agent_board.nudger.signing import verify_nudge_signature

logger = logging.getLogger("agent_board_admin_bot")

_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_.-])@([a-z0-9][a-z0-9_-]{0,48}[a-z0-9]?)")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}[a-z0-9]?$")

_FORBIDDEN_RE = re.compile(
    r"(?is)"
    r"(?:"
    r"\b(?:create|mint|issue|rotate|revoke|delete)[-\s]+(?:api[-\s]+)?tokens?\b"
    r"|\b(?:create|add|delete|disable|enable)[-\s]+principals?\b"
    r"|\b(?:create|add|register|delete|remove)[-\s]+(?:webhook|subscription)s?\b"
    r"|\badmin\s+shout\b|\bbroadcast\s+shout\b"
    r"|\b(?:role\s*=?\s*owner|--role\s+owner|\bas\s+owner\b|\bmake\b.+\bowner\b|\bgrant\b.+\bowner\b)"
    r"|\b(?:mcp_config\.json|active_conversations\.json|ssh\s+into|workstation\s+files?)\b"
    r")"
)

_ALLOWED_SCHEMA_ACTIONS = frozenset(
    {
        "help",
        "status",
        "list_boards",
        "list_members",
        "list_webhooks",
        "create_board",
        "set_membership",
        "remove_membership",
        "set_tags",
        "rename_principal",
        "enable_webhooks",
        "run_backup",
        "refuse",
        "unknown",
    }
)


@dataclass(frozen=True, slots=True)
class AdminBotConfig:
    """Runtime configuration loaded from TOML for `agent-board admin-bot`."""

    board_url: str
    board_token: str
    handle: str = "board-admin"
    webhook_bind: str = "127.0.0.1:8793"
    webhook_bearer: str = ""
    webhook_hmac_secret: str = ""
    ollama_url: str = ""
    ollama_model: str = "gemma4:12b"
    allowed_callers: tuple[str, ...] = ()
    allowed_tags: tuple[str, ...] = ("dev",)
    allow_server_owners: bool = True
    poll_interval_s: float = 120.0

    def parse_webhook_bind(self) -> tuple[str, int]:
        raw = self.webhook_bind.strip()
        if ":" in raw:
            host, _, port_str = raw.rpartition(":")
            host = host.strip() or "127.0.0.1"
            port = int(port_str) if port_str.strip().isdigit() else 8793
            return host, port
        return raw or "127.0.0.1", 8793


def load_admin_bot_config(path: str | Path) -> AdminBotConfig:
    """Load `AdminBotConfig` from a TOML file."""
    raw = tomllib.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    board_url = str(raw.get("board_url", "http://127.0.0.1:8790")).strip().rstrip("/")
    board_token = str(raw.get("board_token", "")).strip()
    if not board_token:
        raise ValueError("board_token must be set in admin-bot config")

    allowed_callers = tuple(
        str(x).strip().lstrip("@").lower() for x in (raw.get("allowed_callers") or []) if str(x).strip()
    )
    raw_tags = raw["allowed_tags"] if "allowed_tags" in raw else ["dev"]
    allowed_tags = tuple(str(x).strip().lower() for x in raw_tags if str(x).strip())
    return AdminBotConfig(
        board_url=board_url,
        board_token=board_token,
        handle=str(raw.get("handle", "board-admin")).strip().lstrip("@").lower() or "board-admin",
        webhook_bind=str(raw.get("webhook_bind", "127.0.0.1:8793")).strip() or "127.0.0.1:8793",
        webhook_bearer=str(raw.get("webhook_bearer", "")).strip(),
        webhook_hmac_secret=str(raw.get("webhook_hmac_secret", "")).strip(),
        ollama_url=str(raw.get("ollama_url", "")).strip().rstrip("/"),
        ollama_model=str(raw.get("ollama_model", "gemma4:12b")).strip() or "gemma4:12b",
        allowed_callers=allowed_callers,
        allowed_tags=allowed_tags,
        allow_server_owners=bool(raw.get("allow_server_owners", True)),
        poll_interval_s=max(5.0, float(raw.get("poll_interval_s", 120.0))),
    )


def _strip_handle_prefix(text: str, bot_handle: str | None) -> str:
    cleaned = text.strip()
    if bot_handle:
        pat = re.compile(rf"^\s*@{re.escape(bot_handle)}(?![A-Za-z0-9_-])[\s,:;-]*", re.IGNORECASE)
        cleaned = pat.sub("", cleaned, count=1).strip()
    return cleaned


def _clean_handle(raw: str) -> str:
    return raw.strip().lstrip("@").rstrip(",.;:").lower()


def _clean_slug(raw: str) -> str:
    return raw.strip().strip("`'\"").rstrip(",.;:").lower()


def _neutralize_mentions(text: str) -> str:
    if not text:
        return ""
    return _MENTION_RE.sub(lambda m: f"[at]{m.group(1)}", text)


def _safe_shlex(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _parse_create_board(tokens: list[str]) -> list[dict[str, Any]] | None:
    if len(tokens) < 2:
        return None
    slug = _clean_slug(tokens[1])
    if not slug or not _SLUG_RE.match(slug):
        return None

    name = ""
    purpose = ""
    members: list[str] = []
    positional: list[str] = []
    idx = 2
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--name" and idx + 1 < len(tokens):
            name = tokens[idx + 1].strip()
            idx += 2
        elif tok.startswith("--name="):
            name = tok.split("=", 1)[1].strip()
            idx += 1
        elif tok == "--purpose" and idx + 1 < len(tokens):
            purpose = tokens[idx + 1].strip()
            idx += 2
        elif tok.startswith("--purpose="):
            purpose = tok.split("=", 1)[1].strip()
            idx += 1
        elif tok == "--add" and idx + 1 < len(tokens):
            members.extend(_clean_handle(m) for m in tokens[idx + 1].split(",") if _clean_handle(m))
            idx += 2
        elif tok.startswith("--add="):
            members.extend(_clean_handle(m) for m in tok.split("=", 1)[1].split(",") if _clean_handle(m))
            idx += 1
        elif tok.startswith("--"):
            return None
        else:
            positional.append(tok)
            idx += 1

    if not name and positional:
        name = positional[0].strip()
        positional = positional[1:]
    if not purpose and positional:
        purpose = positional[0].strip()
        positional = positional[1:]
    if positional:
        return None
    if not name:
        name = slug.replace("-", " ").title()
    if not purpose:
        purpose = f"Coordination board for {name}."
    return [
        {
            "action": "create_board",
            "board_slug": slug,
            "name": name,
            "purpose": purpose,
            "members": members,
        }
    ]


def _parse_rename(tokens: list[str]) -> list[dict[str, Any]] | None:
    if len(tokens) < 3:
        return None
    target = _clean_handle(tokens[1])
    if not target:
        return None
    new_handle = ""
    display_name = ""
    idx = 2
    if idx < len(tokens) and tokens[idx].lower() == "to":
        if idx + 1 >= len(tokens):
            return None
        new_handle = _clean_handle(tokens[idx + 1])
        idx += 2
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--new-handle" and idx + 1 < len(tokens):
            new_handle = _clean_handle(tokens[idx + 1])
            idx += 2
        elif tok.startswith("--new-handle="):
            new_handle = _clean_handle(tok.split("=", 1)[1])
            idx += 1
        elif tok == "--name" and idx + 1 < len(tokens):
            display_name = tokens[idx + 1].strip()
            idx += 2
        elif tok.startswith("--name="):
            display_name = tok.split("=", 1)[1].strip()
            idx += 1
        else:
            return None
    if not new_handle and not display_name:
        return None
    return [
        {
            "action": "rename_principal",
            "target_handle": target,
            "new_handle": new_handle,
            "name": display_name,
        }
    ]


def parse_deterministic_commands(text: str, *, bot_handle: str | None = None) -> list[dict[str, Any]]:
    """Parse explicit deterministic admin commands without invoking any LLM."""
    cleaned = _strip_handle_prefix(text, bot_handle)
    if not cleaned:
        return []
    if _FORBIDDEN_RE.search(cleaned):
        return [
            {
                "action": "refuse",
                "reason": (
                    "Refused: token management, principal creation, webhook registration/deletion, "
                    "owner role grants, cross-board shout, and workstation changes require the human server owner."
                ),
            }
        ]

    lower = cleaned.rstrip("?.!").strip().lower()
    if lower in ("help", "--help", "?"):
        return [{"action": "help"}]
    if lower in ("status", "health"):
        return [{"action": "status"}]
    if lower in ("list boards", "list-boards", "boards"):
        return [{"action": "list_boards"}]
    if lower in ("list webhooks", "list-webhooks", "webhooks", "list subscriptions", "subscriptions"):
        return [{"action": "list_webhooks"}]
    if lower in ("enable webhooks", "enable-webhooks", "heal webhooks", "heal-webhooks"):
        return [{"action": "enable_webhooks"}]
    if lower in ("backup", "run backup", "run-backup"):
        return [{"action": "run_backup"}]

    m_members = re.match(r"^(?:list[\s-]+members|members)\s+([a-z0-9][a-z0-9-]*)$", lower)
    if m_members:
        return [{"action": "list_members", "board_slug": m_members.group(1)}]

    m_enable = re.match(r"^(?:enable[\s-]+webhook|heal[\s-]+webhook)\s+([A-Za-z0-9]+)$", cleaned.strip(), re.I)
    if m_enable:
        return [{"action": "enable_webhooks", "subscription_id": m_enable.group(1)}]

    m_add = re.match(
        r"^add\s+@?([a-z0-9][a-z0-9_-]*)\s+to\s+([a-z0-9][a-z0-9-]*)(?:\s+(agent|readonly|owner))?$",
        lower,
    )
    if m_add:
        role = m_add.group(3) or "agent"
        if role == "owner":
            return [{"action": "refuse", "reason": "Cannot grant 'owner' role via @board-admin; ask the server owner."}]
        return [
            {
                "action": "set_membership",
                "target_handle": m_add.group(1),
                "board_slug": m_add.group(2),
                "role": role,
            }
        ]

    m_set_mem = re.match(
        r"^set[\s-]+membership\s+([a-z0-9][a-z0-9-]*)\s+@?([a-z0-9][a-z0-9_-]*)(?:\s+(agent|readonly|owner))?$",
        lower,
    )
    if m_set_mem:
        role = m_set_mem.group(3) or "agent"
        if role == "owner":
            return [{"action": "refuse", "reason": "Cannot grant 'owner' role via @board-admin; ask the server owner."}]
        return [
            {
                "action": "set_membership",
                "board_slug": m_set_mem.group(1),
                "target_handle": m_set_mem.group(2),
                "role": role,
            }
        ]

    m_rm = re.match(
        r"^(?:remove\s+@?([a-z0-9][a-z0-9_-]*)\s+from\s+([a-z0-9][a-z0-9-]*)"
        r"|remove[\s-]+membership\s+([a-z0-9][a-z0-9-]*)\s+@?([a-z0-9][a-z0-9_-]*))$",
        lower,
    )
    if m_rm:
        if m_rm.group(1) and m_rm.group(2):
            target, board = m_rm.group(1), m_rm.group(2)
        else:
            board, target = m_rm.group(3), m_rm.group(4)
        return [{"action": "remove_membership", "target_handle": target, "board_slug": board}]

    m_clear_tags = re.match(r"^clear[\s-]+tags\s+@?([a-z0-9][a-z0-9_-]*)$", lower)
    if m_clear_tags:
        return [{"action": "set_tags", "target_handle": m_clear_tags.group(1), "tags": []}]

    m_set_tags = re.match(r"^set[\s-]+tags\s+@?([a-z0-9][a-z0-9_-]*)\s+(.+)$", lower)
    if m_set_tags:
        raw_tags = re.split(r"[\s,]+", m_set_tags.group(2).strip())
        tags = [t.strip() for t in raw_tags if t.strip()]
        if tags:
            return [{"action": "set_tags", "target_handle": m_set_tags.group(1), "tags": tags}]

    tokens = _safe_shlex(cleaned)
    if not tokens:
        return []
    head = tokens[0].lower()
    if head in ("create-board", "create_board"):
        parsed = _parse_create_board(["create-board", *tokens[1:]])
        return parsed or []
    if head == "create" and len(tokens) >= 2 and tokens[1].lower() == "board":
        parsed = _parse_create_board(["create-board", *tokens[2:]])
        return parsed or []
    if head in ("rename", "rename-principal"):
        parsed = _parse_rename(tokens)
        return parsed or []

    return []


def load_admin_bot_prompts() -> dict[str, Any]:
    """Load centralized declarative prompts and schemas from `admin_bot_prompts.json`."""
    raw = importlib.resources.files("agent_board").joinpath("admin_bot_prompts.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or "intent_parse" not in data:
        raise ValueError("admin_bot_prompts.json must contain an 'intent_parse' entry")
    return data


class OllamaIntentParser:
    """Structured intent parser backed by local Ollama `/api/chat` and declarative JSON schema."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = 45.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.prompts = load_admin_bot_prompts()
        kwargs: dict[str, Any] = {"base_url": self.base_url, "timeout": timeout_s}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.Client(**kwargs)

    def close(self) -> None:
        self._client.close()

    def health_check(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "not configured"
        try:
            resp = self._client.get("/api/tags", timeout=5.0)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            return False, f"unreachable ({exc.__class__.__name__})"
        models = [str(m.get("name", "")) for m in payload.get("models", []) if isinstance(m, dict)]
        if self.model in models or f"{self.model}:latest" in models:
            return True, f"ready ({self.model})"
        return False, f"model '{self.model}' not loaded"

    def parse(self, *, requester: str, request_text: str, visible_boards: list[str]) -> list[dict[str, Any]]:
        spec = self.prompts["intent_parse"]
        opts = dict(spec.get("options") or {})
        think = opts.pop("think", False)
        user_msg = str(spec["user_template"]).format(
            requester=requester,
            boards=", ".join(visible_boards) if visible_boards else "(none)",
            request_text=request_text,
        )
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "think": think,
            "messages": [
                {"role": "system", "content": str(spec["system"])},
                {"role": "user", "content": user_msg},
            ],
            "format": spec["response_schema"],
        }
        if opts:
            body["options"] = opts

        resp = self._client.post("/api/chat", json=body)
        resp.raise_for_status()
        raw_content = str(resp.json()["message"]["content"]).strip()
        parsed = json.loads(raw_content)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("actions"), list):
            return [{"action": "unknown"}]
        out: list[dict[str, Any]] = []
        for item in parsed["actions"]:
            if not isinstance(item, dict):
                continue
            act = str(item.get("action", "unknown")).strip().lower()
            if act not in _ALLOWED_SCHEMA_ACTIONS:
                out.append({"action": "refuse", "reason": f"Unsupported action '{act}'."})
            else:
                out.append(item)
        return out or [{"action": "unknown"}]


class AdminBotClient:
    """Thin authenticated REST client wrapping Agent Board API endpoints for the admin bot."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _json(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            detail = resp.text
            try:
                err_obj = resp.json().get("error")
                if isinstance(err_obj, dict) and err_obj.get("message"):
                    detail = str(err_obj["message"])
            except Exception:
                pass
            raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
        return resp.json()

    def me(self) -> dict[str, Any]:
        return self._json(self._client.get("/me"))

    def list_boards(self) -> list[dict[str, Any]]:
        return list(self._json(self._client.get("/boards")).get("boards", []))

    def read_messages(self, board_slug: str, *, since_seq: int | None = None, limit: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if since_seq is not None:
            params["since_seq"] = since_seq
        return self._json(self._client.get(f"/boards/{board_slug}/messages", params=params))

    def post_reply(
        self,
        board_slug: str,
        *,
        thread_id: str,
        body_md: str,
        reply_to_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._json(
            self._client.post(
                f"/boards/{board_slug}/messages",
                headers={"Idempotency-Key": idempotency_key},
                json={
                    "thread_id": thread_id,
                    "type": "note",
                    "body_md": body_md,
                    "reply_to_id": reply_to_id,
                },
            )
        )

    def ack(self, board_slug: str, read_seq: int) -> dict[str, Any]:
        return self._json(self._client.post(f"/boards/{board_slug}/ack", json={"read_seq": read_seq}))

    def list_principals(self) -> list[dict[str, Any]]:
        return list(self._json(self._client.get("/admin/principals")).get("principals", []))

    def list_memberships(self) -> list[dict[str, Any]]:
        return list(self._json(self._client.get("/admin/memberships")).get("memberships", []))

    def create_board(self, *, slug: str, name: str, purpose: str) -> dict[str, Any]:
        return self._json(self._client.post("/boards", json={"slug": slug, "name": name, "purpose": purpose}))

    def set_membership(self, *, board_slug: str, principal_handle: str, role: str) -> dict[str, Any]:
        return self._json(
            self._client.post(
                "/admin/memberships",
                json={"board_slug": board_slug, "principal_handle": principal_handle, "role": role},
            )
        )

    def remove_membership(self, *, board_slug: str, principal_handle: str) -> dict[str, Any]:
        return self._json(self._client.delete(f"/admin/memberships/{board_slug}/{principal_handle}"))

    def set_tags(self, *, handle: str, tags: list[str]) -> dict[str, Any]:
        return self._json(self._client.put(f"/admin/principals/{handle}/tags", json={"tags": tags}))

    def rename_principal(
        self,
        *,
        handle: str,
        new_handle: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if new_handle:
            payload["new_handle"] = new_handle
        if display_name:
            payload["display_name"] = display_name
        return self._json(self._client.patch(f"/admin/principals/{handle}", json=payload))

    def list_subscriptions(self) -> list[dict[str, Any]]:
        return list(self._json(self._client.get("/subscriptions", params={"all": "true"})).get("subscriptions", []))

    def enable_subscription(self, sub_id: str) -> dict[str, Any]:
        return self._json(self._client.post(f"/subscriptions/{sub_id}/enable"))

    def backup_status(self) -> dict[str, Any]:
        return self._json(self._client.get("/admin/backups"))

    def run_backup(self) -> dict[str, Any]:
        return self._json(self._client.post("/admin/backups/run"))


class AdminBotProcessor:
    """Enforces caller policy and executes allowlisted Agent Board admin operations."""

    def __init__(
        self,
        config: AdminBotConfig,
        board_client: AdminBotClient,
        ollama_parser: OllamaIntentParser | None = None,
    ) -> None:
        self.config = config
        self.board = board_client
        self.ollama = ollama_parser
        self.bot_handle = config.handle.strip().lower()
        self._direct_mention_re = re.compile(
            rf"(?<![A-Za-z0-9_.-])@{re.escape(self.bot_handle)}(?![A-Za-z0-9_-])",
            re.IGNORECASE,
        )

    def _is_directly_addressed(self, body_md: str) -> bool:
        return bool(self._direct_mention_re.search(body_md or ""))

    def _reply(self, board_slug: str, msg: dict[str, Any], text: str) -> dict[str, Any]:
        author = _clean_handle(str(msg.get("author_handle", "")))
        safe = _neutralize_mentions(text.strip())
        full = f"@{author} {safe}" if author else safe
        return self.board.post_reply(
            board_slug,
            thread_id=str(msg["thread_id"]),
            body_md=full,
            reply_to_id=str(msg["id"]),
            idempotency_key=f"board-admin-reply-{board_slug}-{msg['id']}",
        )

    def _classify_caller(
        self,
        author_handle: str,
        principals_by_handle: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any] | None]:
        principal = principals_by_handle.get(author_handle)
        if principal is None or principal.get("kind") == "system" or principal.get("disabled_at") is not None:
            return "denied", principal
        if bool(principal.get("is_server_owner")) and self.config.allow_server_owners:
            return "owner", principal
        caller_tags = {str(t).lower() for t in (principal.get("tags") or [])}
        if author_handle in self.config.allowed_callers or bool(caller_tags & set(self.config.allowed_tags)):
            return "trusted", principal
        return "member", principal

    def _format_help(self, tier: str) -> str:
        lines = [
            f"Allowlisted commands for `{tier}` callers:",
            "- `status`, `list boards`, `list members <board>`, `list webhooks`",
        ]
        if tier in ("owner", "trusted"):
            lines.append("- `create board <slug> --name \"<Name>\" --purpose \"<Purpose>\" [--add <handle1,handle2>]`")
            lines.append("- `add <handle> to <board> [agent|readonly]`")
            lines.append("- `enable webhooks` or `enable webhook <sub_id>`")
        if tier == "owner":
            lines.append("- `remove <handle> from <board>`, `set-tags <handle> <tag1,tag2>`, `clear-tags <handle>`")
            lines.append("- `rename <handle> --new-handle <new> [--name \"<Display Name>\"]`, `run backup`")
        lines.append(
            "Restricted actions (minting/revoking tokens, creating principals, registering/deleting webhook URLs, "
            "granting `owner`, `shout`, or workstation file changes) must be performed by the human server owner."
        )
        return "\n".join(lines)

    def _execute_action(
        self,
        action: dict[str, Any],
        *,
        tier: str,
        author_handle: str,
        principals_by_handle: dict[str, dict[str, Any]],
        memberships: list[dict[str, Any]],
        caller_boards: set[str],
        newly_created_boards: set[str],
    ) -> str:
        kind = str(action.get("action", "unknown")).strip().lower()
        if kind == "help":
            return self._format_help(tier)
        if kind == "refuse":
            reason = str(action.get("reason", "")).strip()
            return reason or "Refused: that operation requires the human server owner."
        if kind == "unknown":
            return f"Unrecognized command. Run `{self.bot_handle} help` or `{self.bot_handle} status`."

        if kind == "status":
            all_subs = self.board.list_subscriptions()
            all_boards = self.board.list_boards()
            if tier == "owner":
                disabled_subs = [s["id"] for s in all_subs if not s.get("active", True)]
                backup = self.board.backup_status()
                ollama_note = "disabled"
                if self.ollama is not None:
                    _, ollama_note = self.ollama.health_check()
                return (
                    "**Server Status**\n"
                    f"- Active boards: {len([b for b in all_boards if b.get('archived_at') is None])}\n"
                    f"- Subscriptions: {len(all_subs)} total ({len(disabled_subs)} disabled)\n"
                    f"- Ollama intent parser: {ollama_note}\n"
                    f"- Backups: {len(backup.get('backups', []))} on disk"
                )
            if tier == "trusted":
                visible_boards = sorted(caller_boards)
                disabled_subs = [s["id"] for s in all_subs if not s.get("active", True)]
                own_subs = [s for s in all_subs if s.get("principal_handle") == author_handle]
                return (
                    "**Scoped Status**\n"
                    f"- Your boards ({len(visible_boards)}): {', '.join(visible_boards) or '(none)'}\n"
                    f"- Your subscriptions: {len(own_subs)}\n"
                    f"- Auto-disabled server subscriptions: {len(disabled_subs)}"
                )
            own_subs = [s for s in all_subs if s.get("principal_handle") == author_handle]
            return (
                "**Member Status**\n"
                f"- Your boards ({len(caller_boards)}): {', '.join(sorted(caller_boards)) or '(none)'}\n"
                f"- Your subscriptions: {len(own_subs)}"
            )

        if kind == "list_boards":
            all_boards = self.board.list_boards()
            visible = (
                all_boards
                if tier == "owner"
                else [b for b in all_boards if str(b.get("slug", "")).lower() in caller_boards]
            )
            if not visible:
                return "No accessible boards found."
            rows = [f"- `{b['slug']}` — {b['name']}" for b in visible]
            return "**Boards**\n" + "\n".join(rows)

        if kind == "list_members":
            slug = _clean_slug(str(action.get("board_slug", "")))
            if not slug:
                return "Specify a board slug (for example: `list members my-project`)."
            if tier != "owner" and slug not in caller_boards:
                return f"Refused: you are not a member of `{slug}`."
            rows = [m for m in memberships if str(m.get("board_slug", "")).lower() == slug]
            if not rows:
                return f"No members found on `{slug}` (or board does not exist)."
            parts = []
            for m in rows:
                p = principals_by_handle.get(str(m["principal_handle"]).lower(), {})
                tags = p.get("tags") or []
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                parts.append(f"- `{m['principal_handle']}` ({m['role']}){tag_str}")
            return f"**Members on `{slug}`**\n" + "\n".join(parts)

        if kind == "list_webhooks":
            subs = self.board.list_subscriptions()
            visible = (
                subs
                if tier == "owner"
                else [s for s in subs if str(s.get("principal_handle", "")).lower() == author_handle]
            )
            if not visible:
                return "No matching webhook subscriptions found."
            lines = []
            for s in visible:
                state_str = "active" if s.get("active", True) else f"DISABLED ({s.get('disabled_reason') or 'unknown'})"
                lines.append(
                    f"- `{s['id']}` (`{s['principal_handle']}`, boards={s.get('boards')}, {state_str}, "
                    f"failures={s.get('failure_count', 0)})"
                )
            return "**Webhook Subscriptions**\n" + "\n".join(lines)

        if kind == "create_board":
            if tier not in ("owner", "trusted"):
                return "Refused: creating boards requires `dev` tag or server owner permissions."
            slug = _clean_slug(str(action.get("board_slug", "")))
            if not slug or not _SLUG_RE.match(slug):
                return f"Invalid board slug `{slug}`."
            name = str(action.get("name") or slug.replace("-", " ").title()).strip()
            purpose = str(action.get("purpose") or f"Coordination board for {name}.").strip()
            self.board.create_board(slug=slug, name=name, purpose=purpose)
            newly_created_boards.add(slug)
            caller_boards.add(slug)

            raw_members = action.get("members") or []
            requested_handles = [_clean_handle(str(m)) for m in raw_members if _clean_handle(str(m))]
            if tier == "trusted" and author_handle not in requested_handles:
                requested_handles.append(author_handle)
            added: list[str] = []
            skipped: list[str] = []
            for h in dict.fromkeys(requested_handles):
                target = principals_by_handle.get(h)
                if target is None or target.get("kind") == "system" or target.get("disabled_at") is not None:
                    skipped.append(h)
                    continue
                if bool(target.get("is_server_owner")):
                    continue
                self.board.set_membership(board_slug=slug, principal_handle=h, role="agent")
                added.append(h)
            extra = f" Added members: {', '.join(added)}." if added else ""
            skip_note = f" Skipped unknown/disabled: {', '.join(skipped)}." if skipped else ""
            return f"Created board `{slug}` (`{name}`).{extra}{skip_note}"

        if kind == "set_membership":
            if tier not in ("owner", "trusted"):
                return "Refused: managing memberships requires `dev` tag or server owner permissions."
            slug = _clean_slug(str(action.get("board_slug", "")))
            target_h = _clean_handle(str(action.get("target_handle", "")))
            role = str(action.get("role") or "agent").strip().lower()
            if role == "owner":
                return "Refused: cannot grant `owner` role via bot; ask the human server owner."
            if role not in ("agent", "readonly"):
                return f"Invalid role `{role}`; must be `agent` or `readonly`."
            if not slug or not target_h:
                return "Specify both board slug and handle."
            target = principals_by_handle.get(target_h)
            if target is None or target.get("kind") == "system" or target.get("disabled_at") is not None:
                return f"Principal `{target_h}` not found or disabled."
            if tier != "owner" and slug not in caller_boards and slug not in newly_created_boards:
                return (
                    f"Refused: you can only add members to newly created boards or boards you belong to "
                    f"(`{slug}` denied)."
                )
            self.board.set_membership(board_slug=slug, principal_handle=target_h, role=role)
            if target_h == author_handle:
                caller_boards.add(slug)
            return f"Added `{target_h}` to `{slug}` as `{role}` (active token scope synced)."

        if kind == "remove_membership":
            if tier != "owner":
                return "Refused: removing memberships requires a server owner."
            slug = _clean_slug(str(action.get("board_slug", "")))
            target_h = _clean_handle(str(action.get("target_handle", "")))
            target = principals_by_handle.get(target_h)
            if target is None:
                return f"Principal `{target_h}` not found."
            if target_h == self.bot_handle or bool(target.get("is_server_owner")):
                return f"Refused: cannot remove `{target_h}` via bot."
            self.board.remove_membership(board_slug=slug, principal_handle=target_h)
            return f"Removed `{target_h}` from `{slug}`."

        if kind == "set_tags":
            if tier != "owner":
                return "Refused: updating principal tags requires a server owner."
            target_h = _clean_handle(str(action.get("target_handle", "")))
            if target_h not in principals_by_handle:
                return f"Principal `{target_h}` not found."
            tags = [str(t).strip().lower() for t in (action.get("tags") or []) if str(t).strip()]
            res = self.board.set_tags(handle=target_h, tags=tags)
            return f"Updated tags for `{target_h}`: {res.get('tags', [])}."

        if kind == "rename_principal":
            if tier != "owner":
                return "Refused: renaming principals requires a server owner."
            target_h = _clean_handle(str(action.get("target_handle", "")))
            target = principals_by_handle.get(target_h)
            if target is None:
                return f"Principal `{target_h}` not found."
            if target_h == self.bot_handle or bool(target.get("is_server_owner")):
                return f"Refused: cannot rename `{target_h}` via bot."
            new_h = _clean_handle(str(action.get("new_handle", ""))) or None
            disp = str(action.get("name", "")).strip() or None
            if not new_h and not disp:
                return "Specify `--new-handle` and/or `--name`."
            res = self.board.rename_principal(handle=target_h, new_handle=new_h, display_name=disp)
            return f"Renamed `{target_h}` -> `{res['handle']}` (`{res['display_name']}`)."

        if kind == "enable_webhooks":
            if tier not in ("owner", "trusted"):
                return "Refused: enabling webhooks requires `dev` tag or server owner permissions."
            subs = self.board.list_subscriptions()
            sub_id = str(action.get("subscription_id", "")).strip()
            if sub_id:
                match = next((s for s in subs if s["id"] == sub_id), None)
                if match is None:
                    return f"Subscription `{sub_id}` not found."
                if (
                    tier != "owner"
                    and match.get("active", True)
                    and str(match.get("principal_handle", "")).lower() != author_handle
                ):
                    return f"Refused: `{sub_id}` is already active and belongs to another principal."
                self.board.enable_subscription(sub_id)
                return f"Re-enabled subscription `{sub_id}` (`{match['principal_handle']}`)."

            disabled = [s for s in subs if not s.get("active", True)]
            if not disabled:
                return "All webhook subscriptions are already active."
            healed = []
            for s in disabled:
                self.board.enable_subscription(s["id"])
                healed.append(f"`{s['id']}` (`{s['principal_handle']}`)")
            return f"Re-enabled {len(healed)} disabled subscription(s): {', '.join(healed)}."

        if kind == "run_backup":
            if tier != "owner":
                return "Refused: running server backups requires a server owner."
            res = self.board.run_backup()
            if res.get("ok"):
                return f"Backup completed (`{res.get('detail', 'ok')}`)."
            return f"Backup failed: {res.get('detail', 'unknown error')}"

        return f"Unsupported action `{kind}`."

    def _process_message(self, board_slug: str, msg: dict[str, Any]) -> int | None:
        """Process one unread message on a board; returns posted reply seq or None if skipped."""
        author = _clean_handle(str(msg.get("author_handle", "")))
        body = str(msg.get("body_md", ""))
        if not author or author == self.bot_handle:
            return None
        if not self._is_directly_addressed(body):
            return None

        principals = self.board.list_principals()
        principals_by_handle = {str(p["handle"]).lower(): p for p in principals}
        tier, _ = self._classify_caller(author, principals_by_handle)
        if tier == "denied":
            return None

        memberships = self.board.list_memberships()
        caller_boards = {
            str(m["board_slug"]).lower()
            for m in memberships
            if str(m["principal_handle"]).lower() == author
        }

        actions = parse_deterministic_commands(body, bot_handle=self.bot_handle)
        if not actions and self.ollama is not None:
            stripped = _strip_handle_prefix(body, self.bot_handle)
            visible_boards = (
                sorted({str(b["slug"]) for b in self.board.list_boards()})
                if tier == "owner"
                else sorted(caller_boards)
            )
            try:
                actions = self.ollama.parse(
                    requester=author,
                    request_text=stripped,
                    visible_boards=visible_boards,
                )
            except Exception as exc:
                logger.warning("Ollama intent parser error: %s", exc)
                actions = [
                    {
                        "action": "unknown",
                    }
                ]
        if not actions:
            actions = [{"action": "unknown"}]

        newly_created: set[str] = set()
        results: list[str] = []
        for act in actions:
            try:
                results.append(
                    self._execute_action(
                        act,
                        tier=tier,
                        author_handle=author,
                        principals_by_handle=principals_by_handle,
                        memberships=memberships,
                        caller_boards=caller_boards,
                        newly_created_boards=newly_created,
                    )
                )
            except Exception as exc:
                results.append(f"Error executing `{act.get('action', 'command')}`: {exc}")

        posted = self._reply(board_slug, msg, "\n".join(results))
        return int(posted.get("seq", 0))

    def handle_message(self, board_slug: str, msg: dict[str, Any]) -> bool:
        """Process one unread message on a board; returns True if an admin reply was posted."""
        return self._process_message(board_slug, msg) is not None

    def poll_board(self, board_slug: str) -> int:
        """Read unread messages on one board, execute addressed admin requests, and ack cursor."""
        data = self.board.read_messages(board_slug)
        messages = list(data.get("messages", []))
        if not messages:
            return 0
        handled = 0
        max_seq = 0
        for m in messages:
            seq = int(m.get("seq", 0))
            if seq > max_seq:
                max_seq = seq
            posted_seq = self._process_message(board_slug, m)
            if posted_seq is not None:
                handled += 1
                if posted_seq > max_seq:
                    max_seq = posted_seq
        if max_seq > 0:
            self.board.ack(board_slug, max_seq)
        return handled

    def heal_own_subscription(self) -> None:
        """Ensure the bot's own webhook subscription is re-enabled after downtime."""
        try:
            for sub in self.board.list_subscriptions():
                if str(sub.get("principal_handle", "")).lower() == self.bot_handle and not sub.get("active", True):
                    self.board.enable_subscription(sub["id"])
                    logger.info("Re-enabled self webhook subscription %s", sub["id"])
        except Exception as exc:
            logger.warning("Self subscription heal check failed: %s", exc)

    def poll_all_boards(self) -> int:
        """Sweep all boards with unread activity and return number of handled admin commands."""
        total = 0
        for b in self.board.list_boards():
            if b.get("archived_at") is not None or int(b.get("unread_count", 0)) <= 0:
                continue
            slug = str(b["slug"])
            total += self.poll_board(slug)
        return total


def _make_webhook_handler(
    config: AdminBotConfig,
    ollama: OllamaIntentParser | None,
    work_q: queue.Queue[str | None],
) -> type[BaseHTTPRequestHandler]:
    expected_auth = f"Bearer {config.webhook_bearer}" if config.webhook_bearer else ""
    hmac_secret = config.webhook_hmac_secret

    class AdminWebhookHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.info("%s - %s", self.client_address[0], format % args)

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] == "/healthz":
                ollama_ok: bool | None = None
                ollama_detail = "disabled"
                if ollama is not None:
                    ollama_ok, ollama_detail = ollama.health_check()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "handle": config.handle,
                        "ollama_ok": ollama_ok,
                        "ollama_detail": ollama_detail,
                    },
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/webhook":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
                return
            if content_length < 0 or content_length > 262_144:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_payload_size"})
                return

            raw_body = self.rfile.read(content_length)
            auth_header = (self.headers.get("Authorization") or "").strip()
            if not expected_auth or not hmac.compare_digest(auth_header, expected_auth):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_authorization"})
                return

            if not hmac_secret or not verify_nudge_signature(
                hmac_secret,
                timestamp_header=self.headers.get("X-Board-Timestamp") or "",
                signature_header=self.headers.get("X-Board-Signature") or "",
                raw_body=raw_body,
            ):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_signature"})
                return

            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return

            board = str(payload.get("board", "*")).strip().lower() or "*"
            work_q.put(board)
            self._send_json(HTTPStatus.OK, {"ok": True})

    return AdminWebhookHandler


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_once(
    config: AdminBotConfig,
    *,
    board_client: AdminBotClient | None = None,
    ollama_parser: OllamaIntentParser | None = None,
) -> int:
    """Process all current unread board admin requests once and return handled count."""
    owns_board = board_client is None
    owns_ollama = ollama_parser is None and bool(config.ollama_url)
    b_client = board_client or AdminBotClient(config.board_url, config.board_token)
    o_parser = ollama_parser or (
        OllamaIntentParser(config.ollama_url, config.ollama_model) if config.ollama_url else None
    )
    try:
        proc = AdminBotProcessor(config, b_client, o_parser)
        proc.heal_own_subscription()
        return proc.poll_all_boards()
    finally:
        if owns_board:
            b_client.close()
        if owns_ollama and o_parser is not None:
            o_parser.close()


def run_admin_bot(config: AdminBotConfig) -> int:
    """Run the loopback webhook server, worker queue, and periodic recovery poll loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [board-admin] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    board_client = AdminBotClient(config.board_url, config.board_token)
    ollama = OllamaIntentParser(config.ollama_url, config.ollama_model) if config.ollama_url else None
    processor = AdminBotProcessor(config, board_client, ollama)

    work_q: queue.Queue[str | None] = queue.Queue()
    stop_event = threading.Event()
    poll_lock = threading.Lock()

    def _worker() -> None:
        while True:
            item = work_q.get()
            if item is None:
                work_q.task_done()
                return
            try:
                with poll_lock:
                    if item in ("*", "__test__"):
                        processor.poll_all_boards()
                    else:
                        processor.poll_board(item)
            except Exception:
                logger.exception("Admin bot worker failed processing board=%s", item)
            finally:
                work_q.task_done()

    def _poller() -> None:
        while not stop_event.is_set():
            try:
                with poll_lock:
                    processor.heal_own_subscription()
                    processor.poll_all_boards()
            except Exception as exc:
                logger.warning("Periodic admin bot sweep failed: %s", exc)
            stop_event.wait(config.poll_interval_s)

    host, port = config.parse_webhook_bind()
    handler_cls = _make_webhook_handler(config, ollama, work_q)
    server = _ReusableHTTPServer((host, port), handler_cls)

    def _shutdown(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; shutting down", signum)
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    worker_t = threading.Thread(target=_worker, daemon=True)
    poller_t = threading.Thread(target=_poller, daemon=True)
    worker_t.start()
    poller_t.start()

    logger.info("Listening on http://%s:%s as @%s", host, port, config.handle)
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()
        work_q.put(None)
        worker_t.join(timeout=5.0)
        poller_t.join(timeout=2.0)
        board_client.close()
        if ollama is not None:
            ollama.close()
    return 0
