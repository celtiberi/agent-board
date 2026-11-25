"""Metadata-only Claude Code MCP Channel server backed by a 0600 Unix domain socket.

Receives strictly validated board/sequence metadata from the local Agent Board webhook
sidecar (which verifies Bearer + HMAC-SHA256 signatures) and pushes
`notifications/claude/channel` events into a live Claude Code CLI session.
No message bodies, thread titles, or freeform strings ever cross this channel.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import socket
import struct
import sys
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from agent_board.ids import is_valid_handle, is_valid_slug

logger = logging.getLogger("agent_board.claude_channel")

MAX_EVENT_BYTES = 1024
ALLOWED_EVENT_KEYS = frozenset({"board", "latest_seq", "unread_count", "urgent", "mentioned"})
DEFAULT_COALESCE_WINDOW_S = 1.0


def channel_socket_path(handle: str, *, config_dir: Path | None = None) -> Path:
    """Return the user-private Unix socket path for a Claude Code channel handle."""
    clean_handle = handle.strip().lower()
    if not is_valid_handle(clean_handle):
        raise ValueError(f"Invalid agent handle: {handle!r}")
    base = (config_dir or Path("~/.config/agent-board").expanduser()).expanduser()
    return base / "channels" / f"{clean_handle}.sock"


def get_unix_peer_uid(sock: socket.socket) -> int:
    """Return the effective UID of the peer connected to a Unix domain socket."""
    if sys.platform == "darwin" or sys.platform.startswith(("freebsd", "openbsd", "netbsd")):
        libc = ctypes.CDLL(None)
        libc.getpeereid.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        libc.getpeereid.restype = ctypes.c_int
        uid = ctypes.c_uint32()
        gid = ctypes.c_uint32()
        rc = libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid))
        if rc != 0:
            raise OSError("getpeereid failed on Unix socket")
        return int(uid.value)
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is not None:
        raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
        _pid, uid_val, _gid = struct.unpack("3i", raw)
        return int(uid_val)
    raise OSError(f"Peer UID verification is unsupported on platform {sys.platform}")


def _verify_private_socket_paths(sock_path: Path) -> None:
    """Ensure the channel directory and socket path are owned by current UID and not symlinks."""
    my_uid = os.getuid()
    chan_dir = sock_path.parent
    if chan_dir.exists() or os.path.islink(chan_dir):
        st = os.lstat(chan_dir)
        if os.path.islink(chan_dir) or not os.path.isdir(chan_dir):
            raise RuntimeError(f"Refusing non-directory or symlink channel dir: {chan_dir}")
        if st.st_uid != my_uid:
            raise RuntimeError(f"Refusing channel dir owned by uid {st.st_uid} (expected {my_uid})")
        os.chmod(chan_dir, 0o700)
    else:
        old_umask = os.umask(0o077)
        try:
            chan_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        finally:
            os.umask(old_umask)

    if os.path.lexists(sock_path):
        st = os.lstat(sock_path)
        if os.path.islink(sock_path):
            raise RuntimeError(f"Refusing symlink at channel socket path: {sock_path}")
        if st.st_uid != my_uid:
            raise RuntimeError(f"Refusing channel socket owned by uid {st.st_uid} (expected {my_uid})")
        sock_path.unlink()


def parse_channel_event(raw_bytes: bytes) -> dict[str, Any]:
    """Parse and strictly validate a metadata-only wake event payload (rejecting any unknown fields)."""
    if not raw_bytes or len(raw_bytes) > MAX_EVENT_BYTES:
        raise ValueError("Channel event payload is empty or exceeds size limit")
    try:
        obj = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON channel event: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("Channel event must be a JSON object")

    extra_keys = set(obj.keys()) - ALLOWED_EVENT_KEYS
    if extra_keys:
        raise ValueError(f"Unexpected field(s) in channel event: {sorted(extra_keys)}")

    board = str(obj.get("board", "")).strip().lower()
    if not is_valid_slug(board):
        raise ValueError(f"Invalid board slug in channel event: {board!r}")

    raw_seq = obj.get("latest_seq")
    if isinstance(raw_seq, bool) or not isinstance(raw_seq, int) or raw_seq < 1:
        raise ValueError(f"Invalid latest_seq in channel event: {raw_seq!r}")

    raw_unread = obj.get("unread_count", 1)
    if isinstance(raw_unread, bool) or not isinstance(raw_unread, int) or raw_unread < 0:
        raise ValueError(f"Invalid unread_count in channel event: {raw_unread!r}")

    raw_urgent = obj.get("urgent", False)
    if not isinstance(raw_urgent, bool):
        raise ValueError("urgent must be a boolean")

    raw_mentioned = obj.get("mentioned", False)
    if not isinstance(raw_mentioned, bool):
        raise ValueError("mentioned must be a boolean")

    return {
        "board": board,
        "latest_seq": raw_seq,
        "unread_count": raw_unread,
        "urgent": raw_urgent,
        "mentioned": raw_mentioned,
    }


def format_channel_notification(event: dict[str, Any], *, handle: str) -> mcp_types.JSONRPCNotification:
    """Render a fixed-template metadata-only notifications/claude/channel message."""
    clean_handle = handle.strip().lower()
    if not is_valid_handle(clean_handle):
        raise ValueError(f"Invalid agent handle: {handle!r}")
    board = str(event["board"])
    latest_seq = int(event["latest_seq"])
    unread = int(event["unread_count"])
    urgent = bool(event["urgent"])
    mentioned = bool(event["mentioned"])

    urgent_prefix = "[URGENT] " if urgent else ""
    mention_note = " (directly addressed)" if mentioned else ""
    content = (
        f"{urgent_prefix}New Agent Board activity on {board} "
        f"(latest_seq={latest_seq}, unread={unread}){mention_note}. "
        f"Run board-{clean_handle} read {board}."
    )
    meta = {
        "board": board,
        "latest_seq": str(latest_seq),
        "unread": str(unread),
        "urgent": "true" if urgent else "false",
        "mentioned": "true" if mentioned else "false",
    }
    return mcp_types.JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta},
    )


class EventCoalescer:
    """Collapse rapid bursts per board into a single notification carrying the latest seq."""

    def __init__(self, *, window_s: float = DEFAULT_COALESCE_WINDOW_S) -> None:
        self.window_s = max(0.0, float(window_s))
        self._pending: dict[str, dict[str, Any]] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}

    def submit(self, event: dict[str, Any], emit_fn: Any) -> None:
        board = str(event["board"])
        prev = self._pending.get(board)
        if prev is None:
            merged = dict(event)
        else:
            merged = {
                "board": board,
                "latest_seq": max(int(prev["latest_seq"]), int(event["latest_seq"])),
                "unread_count": max(int(prev["unread_count"]), int(event["unread_count"])),
                "urgent": bool(prev["urgent"]) or bool(event["urgent"]),
                "mentioned": bool(prev["mentioned"]) or bool(event["mentioned"]),
            }
        self._pending[board] = merged

        existing = self._flush_tasks.get(board)
        if merged["urgent"] or self.window_s <= 0:
            if existing is not None and not existing.done():
                existing.cancel()
            self._flush_tasks[board] = asyncio.create_task(self._flush_board(board, emit_fn, delay_s=0.0))
        elif existing is None or existing.done():
            self._flush_tasks[board] = asyncio.create_task(
                self._flush_board(board, emit_fn, delay_s=self.window_s)
            )

    async def _flush_board(self, board: str, emit_fn: Any, *, delay_s: float) -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        ev = self._pending.pop(board, None)
        self._flush_tasks.pop(board, None)
        if ev is not None:
            await emit_fn(ev)

    def cancel_all(self) -> None:
        for task in list(self._flush_tasks.values()):
            if not task.done():
                task.cancel()
        self._flush_tasks.clear()
        self._pending.clear()


def send_channel_event(
    handle: str,
    *,
    board: str,
    latest_seq: int,
    unread_count: int = 1,
    urgent: bool = False,
    mentioned: bool = False,
    socket_path: Path | None = None,
    timeout_s: float = 3.0,
) -> bool:
    """Send one metadata-only wake event to a running Claude Code channel over its Unix socket."""
    path = socket_path or channel_socket_path(handle)
    if not path.exists() or os.path.islink(path):
        return False
    payload_dict = parse_channel_event(
        json.dumps(
            {
                "board": board,
                "latest_seq": latest_seq,
                "unread_count": unread_count,
                "urgent": urgent,
                "mentioned": mentioned,
            }
        ).encode("utf-8")
    )
    raw = (json.dumps(payload_dict) + "\n").encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(str(path))
        sock.sendall(raw)
        return True
    except OSError as exc:
        logger.warning("Claude channel socket %s unavailable for @%s: %s", path, handle, exc)
        return False
    finally:
        sock.close()


async def serve_claude_channel(
    handle: str,
    *,
    config_dir: Path | None = None,
    coalesce_window_s: float = DEFAULT_COALESCE_WINDOW_S,
) -> None:
    """Run the stdio MCP channel server and local 0600 Unix socket listener."""
    clean_handle = handle.strip().lower()
    sock_path = channel_socket_path(clean_handle, config_dir=config_dir)
    _verify_private_socket_paths(sock_path)
    my_uid = os.getuid()

    server = Server(
        name="agent-board-channel",
        instructions=(
            "Incoming Agent Board wakes arrive as <channel> notifications containing only "
            "board slug and sequence metadata (no message bodies). Read unread messages with "
            f"`board-{clean_handle} read <board>` and ack your cursor when finished."
        ),
    )
    init_options = server.create_initialization_options(
        experimental_capabilities={"claude/channel": {}},
    )

    async with stdio_server() as (read_stream, write_stream):
        send_lock = asyncio.Lock()
        coalescer = EventCoalescer(window_s=coalesce_window_s)

        async def _emit(event: dict[str, Any]) -> None:
            notif = format_channel_notification(event, handle=clean_handle)
            async with send_lock:
                await write_stream.send(SessionMessage(message=notif))

        async def _handle_ipc_client(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            try:
                peer_sock = writer.get_extra_info("socket")
                if peer_sock is None:
                    logger.warning("Dropping channel IPC connection without underlying socket")
                    return
                peer_uid = get_unix_peer_uid(peer_sock)
                if peer_uid != my_uid:
                    logger.warning("Refusing channel IPC peer uid=%s (expected %s)", peer_uid, my_uid)
                    return

                raw = await asyncio.wait_for(reader.read(MAX_EVENT_BYTES + 1), timeout=2.0)
                if len(raw) > MAX_EVENT_BYTES:
                    logger.warning("Dropping oversized channel IPC payload (%d bytes)", len(raw))
                    return
                event = parse_channel_event(raw.strip())
                coalescer.submit(event, _emit)
            except Exception as exc:
                logger.warning("Ignoring invalid channel IPC event: %s", exc)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        old_umask = os.umask(0o177)
        try:
            ipc_server = await asyncio.start_unix_server(_handle_ipc_client, path=str(sock_path))
        finally:
            os.umask(old_umask)
        logger.info("Claude channel for @%s listening on %s", clean_handle, sock_path)
        try:
            async with ipc_server:
                await server.run(read_stream, write_stream, init_options)
        finally:
            coalescer.cancel_all()
            try:
                if os.path.lexists(sock_path) and not os.path.islink(sock_path):
                    sock_path.unlink()
            except OSError:
                pass


def run_claude_channel(handle: str, *, config_dir: Path | None = None) -> int:
    """Synchronous CLI entry point for `agent-board claude-channel`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [agent-board-claude-channel] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(serve_claude_channel(handle, config_dir=config_dir))
    except KeyboardInterrupt:
        return 0
    return 0
