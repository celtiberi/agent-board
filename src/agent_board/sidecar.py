"""Workstation webhook listener sidecar waking bound Antigravity conversations via `agentapi`."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import tomllib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from agent_board.constants import ACTIONABLE_MESSAGE_TYPES_SET
from agent_board.nudger.signing import verify_nudge_signature

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent-board-webhook] %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("agent_board_webhook")

RECOVERY_INTERVAL_S = 120.0


def _cfg_dir() -> Path:
    return Path("~/.config/agent-board").expanduser()


def _config_toml_path() -> Path:
    return _cfg_dir() / "config.toml"


def _profiles_dir_path() -> Path:
    return _cfg_dir() / "profiles"


def _webhook_toml_path() -> Path:
    return _cfg_dir() / "webhook.toml"


def _active_conv_json_path() -> Path:
    return _cfg_dir() / "active_conversations.json"


def _state_file_path() -> Path:
    data_dir = os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / "state.json"
    return _cfg_dir() / "listener_state.json"


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _load_configs() -> tuple[dict[str, Any], dict[str, Any]]:
    board_cfg = tomllib.loads(_config_toml_path().read_text(encoding="utf-8"))
    hook_cfg = tomllib.loads(_webhook_toml_path().read_text(encoding="utf-8"))
    return board_cfg, hook_cfg


def _load_active_conversations() -> dict[tuple[str, str], dict[str, str]]:
    path = _active_conv_json_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed reading %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}

    routes: dict[tuple[str, str], dict[str, str]] = {}
    for raw_key, val in raw.items():
        clean_key = str(raw_key).strip().lower()
        if not clean_key or clean_key == "*":
            continue
        if ":" in clean_key:
            key_board, key_handle = clean_key.split(":", 1)
        else:
            key_board, key_handle = clean_key, "admin"

        if isinstance(val, str):
            cid = val.strip()
            if cid and key_board:
                routes[(key_board, key_handle)] = {
                    "board": key_board,
                    "handle": key_handle,
                    "conversation_id": cid,
                    "subscription_id": "",
                }
        elif isinstance(val, dict):
            cid = str(val.get("conversation_id", "")).strip()
            clean_board = str(val.get("board", key_board)).strip().lower()
            clean_handle = str(val.get("handle", key_handle)).strip().lower() or "admin"
            if cid and clean_board:
                routes[(clean_board, clean_handle)] = {
                    "board": clean_board,
                    "handle": clean_handle,
                    "conversation_id": cid,
                    "subscription_id": str(val.get("subscription_id", "")).strip(),
                }
    return routes


def _lookup_route(
    board: str,
    principal: str | None = None,
    subscription_id: str | None = None,
) -> dict[str, str] | None:
    routes = _load_active_conversations()
    clean_board = board.strip().lower()
    clean_principal = (principal or "").strip().lower()
    clean_sub = (subscription_id or "").strip()

    if clean_principal:
        route = routes.get((clean_board, clean_principal))
        if route is not None:
            return route
    if clean_sub:
        for route in routes.values():
            if route["board"] == clean_board and route.get("subscription_id") == clean_sub:
                return route
    if clean_principal:
        wildcard = routes.get(("*", clean_principal))
        if wildcard is not None:
            return {**wildcard, "board": clean_board}
    if clean_sub:
        for route in routes.values():
            if route["board"] == "*" and route.get("subscription_id") == clean_sub:
                return {**route, "board": clean_board}
    if not clean_principal and not clean_sub:
        board_routes = [route for (b, _), route in routes.items() if b == clean_board]
        if len(board_routes) == 1:
            return board_routes[0]
    return None


def _load_agent_credentials(handle: str, default_cfg: dict[str, Any]) -> tuple[str, str]:
    prof_path = _profiles_dir_path() / f"{handle}.toml"
    if prof_path.exists():
        try:
            prof = tomllib.loads(prof_path.read_text(encoding="utf-8"))
            url = str(prof.get("url", default_cfg["url"])).rstrip("/")
            token = str(prof.get("token", "")).strip()
            if token:
                return url, token
        except Exception as exc:
            logger.warning("Failed loading profile %s: %s", prof_path, exc)
    return str(default_cfg["url"]).rstrip("/"), str(default_cfg["token"]).strip()


class ListenerState:
    """Thread-safe persisted dispatch tracking to avoid duplicate wakes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.last_dispatched_seq: dict[str, int] = {}
        self.seen_nudge_ids: list[str] = []
        self.inflight_keys: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.last_dispatched_seq = {str(k): int(v) for k, v in raw.get("last_dispatched_seq", {}).items()}
            self.seen_nudge_ids = [str(x) for x in raw.get("seen_nudge_ids", [])][-200:]
        except Exception as exc:
            logger.warning("Failed to load state from %s: %s", self.path, exc)

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "last_dispatched_seq": self.last_dispatched_seq,
                    "seen_nudge_ids": self.seen_nudge_ids[-200:],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    @staticmethod
    def _route_key(board: str, handle: str) -> str:
        clean_board = board.strip().lower()
        clean_handle = handle.strip().lower()
        return f"{clean_board}:{clean_handle}" if clean_handle else clean_board

    def _get_last_seq_locked(self, board: str, handle: str) -> int:
        rkey = self._route_key(board, handle)
        if rkey in self.last_dispatched_seq:
            return self.last_dispatched_seq[rkey]
        return self.last_dispatched_seq.get(board.strip().lower(), 0)

    def should_enqueue(self, board: str, handle: str, latest_seq: int, nudge_id: str | None) -> bool:
        rkey = self._route_key(board, handle)
        inflight_key = f"{rkey}:{latest_seq}"
        with self.lock:
            if nudge_id and f"{rkey}:{nudge_id}" in self.seen_nudge_ids:
                return False
            if latest_seq <= self._get_last_seq_locked(board, handle):
                return False
            if inflight_key in self.inflight_keys:
                return False
            self.inflight_keys.add(inflight_key)
            return True

    def get_last_seq(self, board: str, handle: str = "") -> int:
        with self.lock:
            return self._get_last_seq_locked(board, handle)

    def release_inflight(self, board: str, handle: str, seq: int, nudge_id: str | None = None) -> None:
        rkey = self._route_key(board, handle)
        inflight_key = f"{rkey}:{seq}"
        with self.lock:
            self.inflight_keys.discard(inflight_key)

    def mark_dispatched(self, board: str, handle: str, seq: int, nudge_id: str | None = None) -> None:
        rkey = self._route_key(board, handle)
        inflight_key = f"{rkey}:{seq}"
        with self.lock:
            self.inflight_keys.discard(inflight_key)
            dirty = False
            cur = self._get_last_seq_locked(board, handle)
            if seq > cur:
                self.last_dispatched_seq[rkey] = seq
                dirty = True
            seen_key = f"{rkey}:{nudge_id}" if nudge_id else None
            if seen_key and seen_key not in self.seen_nudge_ids:
                self.seen_nudge_ids.append(seen_key)
                self.seen_nudge_ids = self.seen_nudge_ids[-400:]
                dirty = True
            if dirty:
                self._save_locked()


def _resolve_agentapi() -> str:
    env_bin = os.environ.get("AGENT_BOARD_AGENTAPI_BIN", "").strip()
    if env_bin:
        return env_bin
    found = shutil.which("agentapi")
    if found:
        return found
    return str(Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi")


def _resolve_board_cli(handle: str) -> str:
    env_bin = os.environ.get("AGENT_BOARD_BIN", "").strip()
    base_bin = env_bin or shutil.which("board") or str(Path.home() / ".local" / "bin" / "board")
    if (_profiles_dir_path() / f"{handle}.toml").exists():
        wrapper = Path(base_bin).parent / f"board-{handle}"
        if wrapper.exists():
            return str(wrapper)
        return f"{base_bin} --profile {handle}"
    return base_bin


def _build_prompt(
    *,
    board: str,
    handle: str,
    latest_seq: int,
    unread_count: int,
    types: list[str],
    mentioned: bool,
    source: str,
) -> str:
    type_str = ", ".join(types) if types else "unknown"
    mention_note = f" You were directly @mentioned (`@{handle}`)." if mentioned else ""
    mcp_name = f"agent-board-{handle}" if (_profiles_dir_path() / f"{handle}.toml").exists() else "agent-board"
    cli_path = _resolve_board_cli(handle)

    return (
        f"Agent Board notification ({source}) for `@{handle}` on board `{board}` "
        f"(latest_seq={latest_seq}, unread={unread_count}, types=[{type_str}]).{mention_note}\n\n"
        f"Please inspect and respond using the `{mcp_name}` MCP server or `{cli_path}` CLI:\n"
        f"1. Read unread messages: `read_messages(board='{board}')` or `{cli_path} read {board}`.\n"
        f"2. Check the brief if needed: `get_brief(board='{board}')` or `{cli_path} brief {board}`.\n"
        f"3. Handle any assigned tasks, questions, issues, or retest reports and post replies "
        f"with `post_message` / `{cli_path} post`.\n"
        f"4. Acknowledge your cursor when finished: `ack(board='{board}', read_seq={latest_seq})` "
        f"or `{cli_path} ack {board} {latest_seq}`."
    )


def _dispatch_to_agentapi(state: ListenerState, item: dict[str, Any]) -> None:
    board = str(item["board"])
    latest_seq = int(item["latest_seq"])
    nudge_id = str(item.get("nudge_id", "")).strip() or None
    principal = str(item.get("principal", "")).strip().lower() or None
    subscription_id = str(item.get("subscription_id", "")).strip() or None

    route = _lookup_route(board, principal, subscription_id)
    if route is None:
        fallback_handle = principal or ""
        state.release_inflight(board, fallback_handle, latest_seq, nudge_id)
        logger.info(
            "No bound conversation for board=%s principal=%s sub=%s; skipping wake",
            board,
            principal,
            subscription_id,
        )
        return

    conv_id = route["conversation_id"]
    handle = route["handle"]
    if latest_seq <= state.get_last_seq(board, handle):
        state.release_inflight(board, handle, latest_seq, nudge_id)
        logger.info("Skipping stale dispatch for board=%s handle=%s seq=%s", board, handle, latest_seq)
        return

    agentapi_bin = _resolve_agentapi()
    title = f"[Agent Board: {board} @{handle}] #{latest_seq}"
    prompt = _build_prompt(
        board=board,
        handle=handle,
        latest_seq=latest_seq,
        unread_count=int(item.get("unread_for_you", 1)),
        types=[str(t) for t in item.get("types", [])],
        mentioned=bool(item.get("mentioned", False)),
        source=str(item.get("source", "webhook")),
    )

    cmd = [agentapi_bin, "send-message", f"--title={title}", conv_id, prompt]
    logger.info("Dispatching send-message to %s (@%s) for %s #%s", conv_id, handle, board, latest_seq)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode == 0:
        state.mark_dispatched(board, handle, latest_seq, nudge_id)
        logger.info("Dispatched send-message %s (@%s) #%s -> %s", board, handle, latest_seq, conv_id)
    else:
        state.release_inflight(board, handle, latest_seq, nudge_id)
        logger.error(
            "agentapi send-message failed (rc=%s conv=%s): %s",
            proc.returncode,
            conv_id,
            proc.stderr.strip(),
        )


def _dispatch_worker(
    work_q: queue.Queue[dict[str, Any] | None],
    state: ListenerState,
) -> None:
    while True:
        item = work_q.get()
        if item is None:
            work_q.task_done()
            return
        try:
            _dispatch_to_agentapi(state, item)
        except Exception:
            state.release_inflight(
                str(item.get("board", "")),
                str(item.get("principal", "")).strip().lower(),
                int(item.get("latest_seq", 0) or 0),
                str(item.get("nudge_id", "")).strip() or None,
            )
            logger.exception("Unhandled error in dispatch worker")
        finally:
            work_q.task_done()


def _recover_one_board(
    client: httpx.Client,
    state: ListenerState,
    work_q: queue.Queue[dict[str, Any] | None],
    *,
    slug: str,
    handle: str,
    sub_id: str,
    mentions_only: bool,
    allowed_types: frozenset[str] | set[str],
) -> None:
    b_resp = client.get(f"/boards/{slug}")
    if b_resp.status_code != 200:
        logger.warning("Recovery /boards/%s for @%s returned %s", slug, handle, b_resp.status_code)
        return
    b = b_resp.json()
    unread_count = int(b.get("unread_count", 0))
    latest_seq = int(b.get("next_seq", 1)) - 1
    if unread_count <= 0 or latest_seq <= state.get_last_seq(slug, handle):
        return

    msg_resp = client.get(f"/boards/{slug}/messages")
    if msg_resp.status_code != 200:
        return
    messages = msg_resp.json().get("messages", [])
    matching: list[dict[str, Any]] = []
    for m in messages:
        if str(m.get("author_handle", "")).strip().lower() == handle:
            continue
        mentions = [str(x).strip().lower() for x in (m.get("mentions") or [])]
        mtype = str(m.get("type", "")).strip()
        if mentions:
            if handle in mentions:
                matching.append(m)
        else:
            if not mentions_only and mtype in allowed_types:
                matching.append(m)
    if not matching:
        return

    max_seq = max(int(m["seq"]) for m in matching)
    catchup_id = f"catchup-{slug}-{handle}-{max_seq}"
    if state.should_enqueue(slug, handle, max_seq, catchup_id):
        work_q.put(
            {
                "board": slug,
                "principal": handle,
                "subscription_id": sub_id,
                "latest_seq": max_seq,
                "unread_for_you": len(matching),
                "types": sorted({str(m["type"]) for m in matching}),
                "mentioned": any(handle in [str(x).lower() for x in m.get("mentions", [])] for m in matching),
                "nudge_id": catchup_id,
                "source": "wake-recovery",
            }
        )


def _recover_missed_activity(
    board_cfg: dict[str, Any],
    hook_cfg: dict[str, Any],
    state: ListenerState,
    work_q: queue.Queue[dict[str, Any] | None],
) -> None:
    routes = _load_active_conversations()
    if not routes:
        return

    owner_url = str(board_cfg["url"]).rstrip("/")
    owner_token = str(board_cfg["token"]).strip()
    tracked_sub_ids = {r["subscription_id"] for r in routes.values() if r.get("subscription_id")}
    cfg_sub_id = str(hook_cfg.get("subscription_id", "")).strip()
    if cfg_sub_id:
        tracked_sub_ids.add(cfg_sub_id)

    subs_by_id: dict[str, dict[str, Any]] = {}
    with httpx.Client(
        base_url=f"{owner_url}/api/v1",
        headers={"Authorization": f"Bearer {owner_token}"},
        timeout=15.0,
    ) as owner_client:
        subs_resp = owner_client.get("/subscriptions", params={"all": "true"})
        if subs_resp.status_code == 200:
            for sub in subs_resp.json().get("subscriptions", []):
                sid = str(sub.get("id", ""))
                if sid:
                    subs_by_id[sid] = sub
                if sid in tracked_sub_ids and not sub.get("active", True):
                    en = owner_client.post(f"/subscriptions/{sid}/enable")
                    logger.info("Re-enabled auto-disabled subscription %s -> HTTP %s", sid, en.status_code)

    explicit_keys = {(b, h) for (b, h) in routes if b != "*"}
    for (_b, _h), route in routes.items():
        slug = route["board"]
        handle = route["handle"]
        sub_id = route.get("subscription_id", "")
        sub_meta = subs_by_id.get(sub_id, {})
        flt = sub_meta.get("filter") if isinstance(sub_meta.get("filter"), dict) else {}
        mentions_only = bool(flt.get("mentions_only", False))
        raw_types = flt.get("types")
        allowed_types = (
            {str(t) for t in raw_types}
            if isinstance(raw_types, list) and raw_types
            else ACTIONABLE_MESSAGE_TYPES_SET
        )

        agent_url, agent_token = _load_agent_credentials(handle, board_cfg)
        with httpx.Client(
            base_url=f"{agent_url}/api/v1",
            headers={"Authorization": f"Bearer {agent_token}"},
            timeout=15.0,
        ) as client:
            if slug == "*":
                b_list = client.get("/boards")
                if b_list.status_code != 200:
                    continue
                sub_boards = [str(x).strip().lower() for x in (sub_meta.get("boards") or [])]
                for b_item in b_list.json().get("boards", []):
                    b_slug = str(b_item.get("slug", "")).strip().lower()
                    if not b_slug or b_item.get("archived_at") is not None:
                        continue
                    if (b_slug, handle) in explicit_keys:
                        continue
                    if sub_boards and "*" not in sub_boards and b_slug not in sub_boards:
                        continue
                    _recover_one_board(
                        client,
                        state,
                        work_q,
                        slug=b_slug,
                        handle=handle,
                        sub_id=sub_id,
                        mentions_only=mentions_only,
                        allowed_types=allowed_types,
                    )
            else:
                _recover_one_board(
                    client,
                    state,
                    work_q,
                    slug=slug,
                    handle=handle,
                    sub_id=sub_id,
                    mentions_only=mentions_only,
                    allowed_types=allowed_types,
                )


def _recovery_loop(
    stop_event: threading.Event,
    board_cfg: dict[str, Any],
    hook_cfg: dict[str, Any],
    state: ListenerState,
    work_q: queue.Queue[dict[str, Any] | None],
) -> None:
    while not stop_event.is_set():
        try:
            _recover_missed_activity(board_cfg, hook_cfg, state, work_q)
        except Exception as exc:
            logger.warning("Sleep/wake recovery check failed: %s", exc)
        stop_event.wait(RECOVERY_INTERVAL_S)


def _make_handler(
    *,
    hook_cfg: dict[str, Any],
    state: ListenerState | None,
    work_q: queue.Queue[dict[str, Any] | None] | None,
    test_once_event: threading.Event | None,
) -> type[BaseHTTPRequestHandler]:
    expected_auth = f"Bearer {str(hook_cfg['bearer_token']).strip()}"
    hmac_secret = str(hook_cfg["hmac_secret"])

    class WebhookHandler(BaseHTTPRequestHandler):
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
                self._send_json(HTTPStatus.OK, {"status": "ok"})
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
            if content_length <= 0 or content_length > 262_144:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_payload_size"})
                return

            raw_body = self.rfile.read(content_length)
            auth_header = (self.headers.get("Authorization") or "").strip()
            if not hmac.compare_digest(auth_header, expected_auth):
                logger.warning("Rejected webhook with invalid Authorization header")
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_authorization"})
                return

            ts_header = self.headers.get("X-Board-Timestamp") or ""
            sig_header = self.headers.get("X-Board-Signature") or ""
            if not verify_nudge_signature(
                hmac_secret,
                timestamp_header=ts_header,
                signature_header=sig_header,
                raw_body=raw_body,
            ):
                logger.warning("Rejected webhook with invalid HMAC signature")
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_signature"})
                return

            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return

            if test_once_event is not None:
                logger.info("Verified test webhook payload: %s", json.dumps(payload))
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": "test-once", "nudge_id": payload.get("nudge_id")})
                test_once_event.set()
                return

            board = str(payload.get("board", "")).strip()
            principal = str(payload.get("principal", "")).strip().lower() or None
            subscription_id = str(payload.get("subscription_id", "")).strip() or None
            latest_seq = int(payload.get("latest_seq", 0) or 0)
            unread = int(payload.get("unread_for_you", 0) or 0)
            nudge_id = str(payload.get("nudge_id", "")).strip() or None

            self._send_json(HTTPStatus.OK, {"ok": True})

            if state is not None and work_q is not None and board and unread > 0:
                route = _lookup_route(board, principal, subscription_id)
                if route is None:
                    logger.info(
                        "No bound conversation for board=%s principal=%s sub=%s; skipping wake",
                        board,
                        principal,
                        subscription_id,
                    )
                    return
                handle = route["handle"]
                if state.should_enqueue(board, handle, latest_seq, nudge_id):
                    work_q.put(
                        {
                            **payload,
                            "principal": handle,
                            "subscription_id": subscription_id or route.get("subscription_id", ""),
                            "source": "webhook",
                        }
                    )

    return WebhookHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Board webhook listener sidecar.")
    parser.add_argument("--test-once", action="store_true", help="Verify one valid webhook request and exit 0.")
    args = parser.parse_args(argv)

    board_cfg, hook_cfg = _load_configs()
    host = str(hook_cfg.get("listen_host", "127.0.0.1"))
    port = int(hook_cfg.get("listen_port", 8795))

    stop_event = threading.Event()
    test_once_event = threading.Event() if args.test_once else None
    state = None if args.test_once else ListenerState(_state_file_path())
    work_q: queue.Queue[dict[str, Any] | None] | None = None if args.test_once else queue.Queue()

    handler_cls = _make_handler(
        hook_cfg=hook_cfg,
        state=state,
        work_q=work_q,
        test_once_event=test_once_event,
    )
    server = ReusableThreadingHTTPServer((host, port), handler_cls)

    def _shutdown_handler(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; shutting down", signum)
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    worker_thread: threading.Thread | None = None
    recovery_thread: threading.Thread | None = None

    if not args.test_once and state is not None and work_q is not None:
        worker_thread = threading.Thread(target=_dispatch_worker, args=(work_q, state), daemon=True)
        worker_thread.start()
        recovery_thread = threading.Thread(
            target=_recovery_loop,
            args=(stop_event, board_cfg, hook_cfg, state, work_q),
            daemon=True,
        )
        recovery_thread.start()
    elif test_once_event is not None:

        def _wait_and_stop() -> None:
            test_once_event.wait()
            server.shutdown()

        threading.Thread(target=_wait_and_stop, daemon=True).start()

    logger.info("Listening on http://%s:%s (test_once=%s)", host, port, args.test_once)
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()
        if work_q is not None:
            work_q.put(None)
        if worker_thread is not None:
            worker_thread.join(timeout=5.0)
        if recovery_thread is not None:
            recovery_thread.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
