"""Shared domain constants across server, CLI, and sidecar."""

from __future__ import annotations

ACTIONABLE_MESSAGE_TYPES: tuple[str, ...] = (
    "task",
    "question",
    "issue",
    "decision",
    "retest_report",
    "status",
)
ACTIONABLE_MESSAGE_TYPES_SET: frozenset[str] = frozenset(ACTIONABLE_MESSAGE_TYPES)
