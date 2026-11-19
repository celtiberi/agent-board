"""ID, timestamp, token, and reference parsing helpers."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ULID_LOCK = threading.Lock()
_LAST_MS = 0
_LAST_RAND = 0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECISION_REF_RE = re.compile(r"^[Dd]-(\d+)$")
_ISSUE_REF_RE = re.compile(r"^[Ii]-(\d+)$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]?$")
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}[a-z0-9]?$")
BROADCAST_MENTIONS = frozenset({"all", "everyone"})
RESERVED_HANDLES = frozenset({"system", *BROADCAST_MENTIONS})


def _encode_base32(value: int, length: int) -> str:
    chars = ["0"] * length
    for idx in range(length - 1, -1, -1):
        chars[idx] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(chars)


def new_ulid() -> str:
    """Generate a thread-safe monotonic 26-char Crockford Base32 ULID."""
    global _LAST_MS, _LAST_RAND
    with _ULID_LOCK:
        now_ms = int(time.time() * 1000)
        if now_ms <= _LAST_MS:
            now_ms = _LAST_MS
            _LAST_RAND = (_LAST_RAND + 1) & ((1 << 80) - 1)
            if _LAST_RAND == 0:
                now_ms += 1
                _LAST_MS = now_ms
        else:
            _LAST_MS = now_ms
            _LAST_RAND = int.from_bytes(os.urandom(10), "big")
        return _encode_base32(now_ms, 10) + _encode_base32(_LAST_RAND, 16)


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(UTC)


def utc_now_iso() -> str:
    """Return current UTC time formatted as ISO-8601 with Z suffix."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def dt_to_iso(dt: datetime) -> str:
    """Format a datetime in UTC ISO-8601 with Z suffix."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_dt(value: str) -> datetime:
    """Parse an ISO-8601 UTC string into an aware UTC datetime."""
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def validate_iso_date(value: str) -> str:
    """Validate strict YYYY-MM-DD calendar date string."""
    if not _DATE_RE.match(value):
        raise ValueError("Date must be in YYYY-MM-DD format")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def generate_api_token() -> tuple[str, str, str]:
    """Generate an ab_ token from 32 random bytes, returning (plaintext, prefix, sha256_hex)."""
    raw = secrets.token_bytes(32)
    num = int.from_bytes(raw, "big")
    chars: list[str] = []
    while num:
        num, rem = divmod(num, 62)
        chars.append(_BASE62[rem])
    token_body = "".join(reversed(chars)).rjust(43, "0")
    plaintext = f"ab_{token_body}"
    prefix = plaintext[:11]
    token_hash = hash_token(plaintext)
    return plaintext, prefix, token_hash


def hash_token(token: str) -> str:
    """Compute SHA-256 hex digest of a token or session secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_decision_ref(ref: str) -> tuple[str, int | str]:
    """Parse 'D-7', '7', or ULID into ('number', 7) or ('id', ulid)."""
    trimmed = ref.strip()
    m = _DECISION_REF_RE.match(trimmed)
    if m:
        return ("number", int(m.group(1)))
    if trimmed.isdigit():
        return ("number", int(trimmed))
    return ("id", trimmed)


def parse_issue_ref(ref: str) -> tuple[str, int | str]:
    """Parse 'I-12', '12', or ULID into ('number', 12) or ('id', ulid)."""
    trimmed = ref.strip()
    m = _ISSUE_REF_RE.match(trimmed)
    if m:
        return ("number", int(m.group(1)))
    if trimmed.isdigit():
        return ("number", int(trimmed))
    return ("id", trimmed)


def is_valid_slug(slug: str) -> bool:
    """Check if a board slug matches allowed lowercase URL characters."""
    return bool(_SLUG_RE.match(slug))


def is_valid_handle(handle: str) -> bool:
    """Check if a principal handle matches allowed @mention characters."""
    return bool(_HANDLE_RE.match(handle))
