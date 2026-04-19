"""Interactive setup wizard for ``agentbus setup``.

This package owns the polished onboarding flow — a linear, sectioned
walk that covers provider/model/tools/memory/channels and ends with a
``doctor`` probe. The TUI layer (``theme``, ``prompter``) is kept
separate from the flow (``wizard``) so tests can run the flow against
a :class:`FakePrompter` with scripted answers and never touch a TTY.

Public entry point: :func:`run_setup`. CLI dispatches to it from
``agentbus.cli``. Programmatic callers (e.g. integration tests) can
pass a custom ``Prompter`` to drive the wizard headlessly.
"""

from __future__ import annotations

from agentbus.setup.prompter import (
    FakePrompter,
    PromptCancelled,
    Prompter,
    QuestionaryPrompter,
)
from agentbus.setup.wizard import run_setup

__all__ = [
    "FakePrompter",
    "PromptCancelled",
    "Prompter",
    "QuestionaryPrompter",
    "run_setup",
]
