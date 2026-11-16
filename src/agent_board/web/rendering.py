"""Markdown sanitization and America/Chicago timestamp formatting for Jinja2 templates."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

_CHICAGO = ZoneInfo("America/Chicago")
_MD = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")

_ALLOWED_TAGS: set[str] = {
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "del",
    "s",
}

_ALLOWED_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title"},
}


def render_markdown_safe(md_text: str | None) -> Markup:
    """Render Markdown to sanitized HTML with no raw HTML and rel=noopener nofollow links."""
    if not md_text:
        return Markup("")
    raw_html = _MD.render(md_text)
    cleaned = nh3.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        link_rel="noopener nofollow",
    )
    return Markup(cleaned)


def render_search_snippet_safe(snippet: str | None) -> Markup:
    """Sanitize FTS5 snippet so only <mark> tags survive."""
    if not snippet:
        return Markup("")
    cleaned = nh3.clean(snippet, tags={"mark"}, attributes={})
    return Markup(cleaned)


def format_chicago_time(iso_str: str | None) -> str:
    """Format a UTC ISO-8601 timestamp string into America/Chicago local time."""
    if not iso_str:
        return "—"
    try:
        from agent_board.ids import iso_to_dt

        dt = iso_to_dt(iso_str).astimezone(_CHICAGO)
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return str(iso_str)
