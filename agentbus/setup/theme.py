"""Visual theme for the setup wizard — banner, palette, layout helpers.

Rendering is deliberately plain ANSI so the theme module has no hard
dependency on rich. The wizard calls ``render_banner`` / ``render_note``
via the :class:`~agentbus.setup.prompter.Prompter`, which may route
through rich when available. Keeping the glyph + color math here means
tests and the fake prompter produce the same textual output as the
interactive one minus the ANSI codes.

Palette notes:

* ``ACCENT`` is AgentBus cyan — used for the banner and section dividers.
* ``SUCCESS`` / ``WARN`` / ``ERROR`` map to the doctor probe ``ok /
  warn / fail`` triple so visual state is consistent end-to-end.
* ``MUTED`` is used for hints, tags, and the "cancelled" footer.

We avoid mid-word color changes — colorizing full lines keeps the
output readable in dumb terminals that strip ANSI (e.g. CI logs).
"""

from __future__ import annotations

import os
import sys

# ── palette (ANSI 16-color codes; friendly to non-truecolor terms) ────────

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

ACCENT = "\033[36m"  # cyan
SUCCESS = "\033[32m"  # green
WARN = "\033[33m"  # yellow
ERROR = "\033[31m"  # red
MUTED = "\033[90m"  # bright black


def supports_color() -> bool:
    """Return True if stdout looks like it'll handle ANSI escapes.

    Honors the ``NO_COLOR`` env var (per no-color.org) and falls back
    to False on non-TTY streams. ``AGENTBUS_FORCE_COLOR=1`` overrides
    for CI runs and test harnesses that capture output.
    """
    if os.environ.get("AGENTBUS_FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def colorize(text: str, color: str, *, bold: bool = False) -> str:
    """Wrap ``text`` in ANSI codes, or return it unchanged on no-color terminals."""
    if not supports_color():
        return text
    prefix = color + (BOLD if bold else "")
    return f"{prefix}{text}{RESET}"


# ── banner ────────────────────────────────────────────────────────────────

_BANNER_LINES = (
    "  █▀█ █▀▀ █▀▀ █▄ █ ▀█▀ █▄▄ █ █ █▀",
    "  █▀█ █▄█ ██▄ █ ▀█  █  █▄█ █▄█ ▄█",
)


def render_banner(version: str | None = None, *, tagline: str = "interactive setup") -> str:
    """Return the AgentBus banner as a multi-line string.

    Line 1/2: block-art logo.  Line 3: version + tagline, dimmed.
    Rendered once at entry; the caller owns printing it so the banner
    appears before any TUI framework takes over the cursor. Callers
    that want a different subtitle (``agentbus chat``, ``agentbus
    launch``, …) pass ``tagline`` — otherwise it matches the setup
    wizard's default wording.
    """
    logo = [colorize(line, ACCENT, bold=True) for line in _BANNER_LINES]
    tagline_parts = []
    if version:
        tagline_parts.append(f"v{version}")
    tagline_parts.append(tagline)
    sub = "  " + colorize(" · ".join(tagline_parts), MUTED)
    return "\n".join([*logo, sub])


# ── line helpers ──────────────────────────────────────────────────────────

_SECTION_BAR = "─"
_TONE_GLYPHS = {
    "info": ("◆", ACCENT),
    "success": ("✓", SUCCESS),
    "warn": ("!", WARN),
    "error": ("✗", ERROR),
    "muted": ("·", MUTED),
}


def render_section(title: str, subtitle: str | None = None) -> str:
    """Heading for a new wizard step, with a faint subtitle below."""
    bar = colorize(_SECTION_BAR * max(2, len(title) + 2), MUTED)
    head = colorize(title, ACCENT, bold=True)
    if subtitle:
        sub = colorize(subtitle, MUTED)
        return f"\n{bar}\n{head}\n{sub}"
    return f"\n{bar}\n{head}"


def render_note(text: str, *, tone: str = "info") -> str:
    """Single-line annotation — successes, warnings, hints."""
    glyph, color = _TONE_GLYPHS.get(tone, _TONE_GLYPHS["info"])
    return f"{colorize(glyph, color)}  {text}"


def render_outro(config_path: str, *, wrote: bool) -> str:
    """Final block shown when the wizard completes or cancels."""
    if wrote:
        head = colorize("Setup complete", SUCCESS, bold=True)
        hint = colorize(f"config: {config_path}", MUTED)
        return f"\n{head}\n{hint}\n" + colorize("next: agentbus chat  ·  agentbus doctor", MUTED)
    return "\n" + colorize("Setup cancelled — nothing written.", MUTED)
