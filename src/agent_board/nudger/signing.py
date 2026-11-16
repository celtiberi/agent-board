"""HMAC-SHA256 signing and verification for nudge webhooks."""

from __future__ import annotations

import hashlib
import hmac
import time


def sign_nudge(secret: str | bytes, timestamp: int, raw_body: bytes) -> str:
    """Compute 'sha256=<hex>' over '<timestamp>.<raw_body>'."""
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    payload = f"{timestamp}.".encode() + raw_body
    digest = hmac.new(secret_bytes, payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_nudge_signature(
    secret: str | bytes,
    *,
    timestamp_header: str,
    signature_header: str,
    raw_body: bytes,
    now_ts: int | None = None,
    max_age_s: int = 300,
) -> bool:
    """Verify X-Board-Timestamp (within max_age_s) and X-Board-Signature."""
    try:
        ts = int(timestamp_header.strip())
    except (ValueError, AttributeError):
        return False
    current = int(time.time()) if now_ts is None else now_ts
    if abs(current - ts) > max_age_s:
        return False
    expected = sign_nudge(secret, ts, raw_body)
    return hmac.compare_digest(expected, signature_header.strip())
