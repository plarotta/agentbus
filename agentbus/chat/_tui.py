"""Interactive chat UI for `agentbus chat` — Claude-Code-style.

Requires: ``uv sync --extra tui`` (prompt_toolkit + rich).

Design: no fullscreen takeover. Normal terminal scrollback is preserved.
Input is a prompt_toolkit prompt with persistent history and a bottom
toolbar; responses stream through rich as rendered Markdown; live tool
dispatches print dimmed inline above the prompt while the user keeps
typing.

Visual language is shared with ``agentbus setup`` — the block-art
banner and cyan/muted palette come from :mod:`agentbus.setup.theme`
so the two surfaces feel like one product. Specifically:

* Banner: :func:`theme.render_banner` with a chat-specific tagline.
* Prompt glyph ``❯`` mirrors questionary's qmark styling (cyan bold).
* Tool dispatch markers: ``↳`` in muted tone (matches the wizard's
  muted notes).
* Toolbar: bold cyan ``AgentBus`` lede + muted body, with yellow
  status verbs while work is in flight.
* Errors use the theme ``✗`` glyph in bold red.

    █▀█ █▀▀ █▀▀ █▄ █ ▀█▀ █▄▄ █ █ █▀
    █▀█ █▄█ ██▄ █ ▀█  █  █▄█ █▄█ ▄█
      v0.1.0 · chat · anthropic/claude-haiku-4-5 · 4 tools
      · type /help for commands, Ctrl-D to exit

    ❯ what's in /etc/hostname
      ↳ file_read
    The hostname file contains `example.local`.

    ❯ ▏
     AgentBus · anthropic/claude-haiku-4-5 · 4 tools · session a1b2c3d4
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown

from agentbus.schemas.common import InboundChat, OutboundChat
from agentbus.schemas.harness import PlannerStatus
from agentbus.setup import theme

from ._commands import CommandResult, handle_command
from ._config import ChatConfig

if TYPE_CHECKING:
    from agentbus.bus import MessageBus

    from ._planner import ChatPlannerNode

_AGENTBUS_VERSION = "0.1.0"
_DEFAULT_HISTORY_PATH = Path.home() / ".agentbus" / "history"
_RESPONSE_TIMEOUT_S = 300.0


class ChatApp:
    """prompt_toolkit + rich chat loop.

    The bus and nodes are owned by the caller (the runner) — this class
    only drives I/O: read one line, publish it, await one response, render.
    PlannerStatus events stream continuously and update the bottom toolbar
    plus print dim inline tool-dispatch markers above the prompt.
    """

    def __init__(
        self,
        config: ChatConfig,
        bus: MessageBus,
        planner: ChatPlannerNode,
        response_queue: asyncio.Queue[OutboundChat],
        status_queue: asyncio.Queue[PlannerStatus],
        *,
        history_path: Path | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._planner = planner
        self._response_queue = response_queue
        self._status_queue = status_queue
        self._console = Console()
        self._current_status: str = ""
        self._awaiting_response: bool = False

        history_path = history_path or _DEFAULT_HISTORY_PATH
        with contextlib.suppress(OSError):
            history_path.parent.mkdir(parents=True, exist_ok=True)

        self._session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
            bottom_toolbar=self._bottom_toolbar,
            refresh_interval=0.5,
        )

    # ── Toolbar ─────────────────────────────────────────────────────────────

    def _bottom_toolbar(self) -> HTML:
        model = f"{self._config.provider}/{self._config.model}"
        tools = len(self._config.tools)
        sid = (
            self._planner.session.session_id[:8]
            if self._planner is not None and self._planner.session is not None
            else "—"
        )
        status = (
            f" <ansiyellow>· {self._current_status}</ansiyellow>" if self._current_status else ""
        )
        # <ansicyan> on "AgentBus" matches the banner accent; the "·" separator
        # mirrors the setup wizard's tagline punctuation so the two surfaces
        # read as one visual family.
        return HTML(
            f" <b><ansicyan>AgentBus</ansicyan></b>"
            f' <style fg="ansibrightblack">· {model} · {tools} tools · session {sid}</style>'
            f"{status}"
        )

    def _invalidate(self) -> None:
        """Redraw the toolbar — safe to call even before the session is running."""
        app = getattr(self._session, "app", None)
        if app is not None:
            with contextlib.suppress(Exception):
                app.invalidate()

    # ── Entry point ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._print_banner()

        status_task = asyncio.create_task(self._drain_status())
        try:
            prompt_fragment = HTML("<b><ansicyan>❯</ansicyan></b> ")
            with patch_stdout(raw=True):
                while True:
                    try:
                        text = await self._session.prompt_async(prompt_fragment)
                    except EOFError:
                        break
                    except KeyboardInterrupt:
                        # Per shell convention: Ctrl-C clears the current line,
                        # Ctrl-D exits. prompt_toolkit raises KeyboardInterrupt
                        # on Ctrl-C — swallow and redraw.
                        continue

                    text = text.strip()
                    if not text:
                        continue

                    if text.startswith("/"):
                        quit_requested = await self._handle_slash(text)
                        if quit_requested:
                            break
                        continue

                    self._bus.publish(
                        "/inbound",
                        InboundChat(channel="tui", sender="user", text=text),
                    )
                    await self._await_response()
        finally:
            status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await status_task

    # ── Rendering ───────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        model = f"{self._config.provider}/{self._config.model}"
        tools = len(self._config.tools)
        tool_word = "tool" if tools == 1 else "tools"
        # Reuse the setup wizard's block-art logo so ``agentbus chat`` and
        # ``agentbus setup`` share a single visual identity. We print the
        # banner string directly (rich.Console would double-escape the
        # embedded ANSI codes) and follow it with a rich-styled hint line.
        print(
            theme.render_banner(
                _AGENTBUS_VERSION,
                tagline=f"chat · {model} · {tools} {tool_word}",
            )
        )
        self._console.print(
            "  [bright_black]· type /help for commands, Ctrl-D to exit[/bright_black]"
        )
        self._console.print()

    async def _drain_status(self) -> None:
        while True:
            status: PlannerStatus = await self._status_queue.get()
            label = self._status_label(status)
            if label is not None:
                self._current_status = label
                self._invalidate()
            # Echo tool dispatches inline only while we're actively waiting
            # for a response — otherwise bus-internal traffic would scroll
            # into the terminal.
            if status.event == "tool_dispatched" and status.tool_name and self._awaiting_response:
                self._console.print(f"  [dim]↳ {status.tool_name}[/dim]")

    @staticmethod
    def _status_label(status: PlannerStatus) -> str | None:
        ev = status.event
        if ev == "thinking":
            return "thinking…"
        if ev == "tool_dispatched" and status.tool_name:
            return f"↳ {status.tool_name}"
        if ev == "tool_received" and status.tool_name:
            return f"← {status.tool_name}"
        if ev == "compacting":
            return "compacting…"
        if ev == "responding":
            return "responding…"
        if ev == "error":
            return f"error: {status.detail or 'unknown'}"
        return None

    async def _await_response(self) -> None:
        self._awaiting_response = True
        self._invalidate()
        try:
            response: OutboundChat = await asyncio.wait_for(
                self._response_queue.get(), timeout=_RESPONSE_TIMEOUT_S
            )
        except TimeoutError:
            self._console.print("[bold red]✗[/bold red] response timed out after 5 minutes")
            return
        finally:
            self._awaiting_response = False
            self._current_status = ""
            self._invalidate()

        self._console.print()
        self._console.print(Markdown(response.text))
        self._console.print()

    async def _handle_slash(self, text: str) -> bool:
        """Run a slash command. Returns True if the session should quit."""
        result: CommandResult = await handle_command(
            text,
            bus=self._bus,
            planner=self._planner,
            config=self._config,
        )
        if result.quit:
            return True
        if result.inspect_toggle is not None:
            self._console.print(
                "[bright_black]· inspect pane is retired. For a live topic feed, run "
                "`agentbus topic echo <topic>` in another terminal.[/bright_black]"
            )
        if result.output is not None:
            self._console.print(result.output)
        if result.error:
            self._console.print(f"[bold red]✗[/bold red] {result.error}")
        return False


async def run_tui_app(
    config: ChatConfig,
    bus: MessageBus,
    planner: ChatPlannerNode,
    response_queue: asyncio.Queue[OutboundChat],
    status_queue: asyncio.Queue[PlannerStatus],
    *,
    history_path: Path | None = None,
) -> None:
    """Convenience wrapper — builds the app and runs the prompt loop."""
    app = ChatApp(
        config,
        bus,
        planner,
        response_queue,
        status_queue,
        history_path=history_path,
    )
    await app.run()
