"""Non-blocking secret pattern scanner for message bodies."""

from __future__ import annotations

import re

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\b(?:token|secret|api_?key|password|bearer)\s*[=:]\s*['\"]?([A-Za-z0-9+/=_-]{24,})['\"]?"),
)

_IGNORE_VALUE_PREFIXES = ("vault:", "env:", "<", "redacted", "example", "placeholder")


def scan_for_secrets(text: str) -> list[str]:
    """Return non-blocking warning strings if text appears to contain a raw secret."""
    if not text:
        return []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            candidate = match.group(1) if match.lastindex else match.group(0)
            lowered = candidate.lower()
            if any(lowered.startswith(prefix) for prefix in _IGNORE_VALUE_PREFIXES):
                continue
            if set(candidate) <= {"*", "x", "X", "."}:
                continue
            return ["possible secret detected"]
    return []
