"""Split long outbound messages at natural boundaries.

Slack rejects messages over ~40k chars and degrades gracefully well
before that; Telegram's ``sendMessage`` hard-caps text at 4096. Without
chunking, a long LLM answer silently truncates or errors. The helper
here cuts the string at the nearest paragraph/newline/space boundary
under ``limit`` so the chunks look like natural message boundaries
rather than a mid-word slice.

Channel gateways import the limit constant they care about and call
:func:`chunk_text` once per outbound message. Short messages return a
single-element list — callers don't need to special-case.
"""

from __future__ import annotations

SLACK_TEXT_LIMIT = 8000
"""Conservative cap — Slack's hard limit is ~40k but long blocks are
visually hostile and hit rate limits faster. 8000 matches openclaw's
choice and is well above typical LLM output."""

TELEGRAM_TEXT_LIMIT = 4096
"""Telegram Bot API hard limit for ``sendMessage``."""


def chunk_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces no longer than ``limit`` characters.

    Prefers to break at paragraph (``\\n\\n``), then line (``\\n``),
    then whitespace. Falls back to a hard cut if no boundary is found
    within the window (e.g. one very long URL). An empty input returns
    an empty list.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    out: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = _find_break(remaining, limit)
        chunk = remaining[:cut].rstrip()
        if chunk:
            out.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out


def _find_break(s: str, limit: int) -> int:
    """Return an index ``i`` (``1 <= i <= limit``) that is a good cut."""
    window = s[:limit]
    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep)
        if idx > 0:
            return idx + len(sep)
    return limit
