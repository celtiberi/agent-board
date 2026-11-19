"""Agent Board: self-hosted multi-project message board for AI agents and human operators."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-board")
except PackageNotFoundError:  # pragma: no cover - editable fallback
    __version__ = "0.2.0"
