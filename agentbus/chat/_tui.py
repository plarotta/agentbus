"""Full textual TUI for agentbus chat (Phase 3).

Requires: pip install textual  (or uv sync --extra tui)

Layout:

    ┌─ AgentBus • provider/model • session ──────────────────┐
    │                                                         │
    │  conversation scroll area                               │
    │                                                         │
    ├─ [status line] ─────────────────────────────────────────┤  ← hidden when idle
    │ >  input                                                │
    └─────────────────────────────────────────────────────────┘

Inspect mode (Ctrl-I or /inspect):

    ┌─ AgentBus ──────────────────────────────────────────────┐
    │  conversation                                           │
    ├─── inspect ─────────────────────────────────────────────┤
    │  live topic feed                                        │
    ├─────────────────────────────────────────────────────────┤
    │ > input                                                 │
    └─────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.message import Message as TUIMessage
from textual.reactive import reactive
from textual.widgets import Input, Log, Static

from agentbus.schemas.common import InboundChat, OutboundChat
from agentbus.schemas.harness import PlannerStatus

from ._commands import CommandResult, handle_command
from ._config import ChatConfig

if TYPE_CHECKING:
    from agentbus.bus import MessageBus

    from ._planner import ChatPlannerNode

_AGENTBUS_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Custom Textual messages (for cross-task communication)
# ---------------------------------------------------------------------------


class ResponseReceived(TUIMessage):
    """Fired when /outbound delivers a response."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class StatusUpdated(TUIMessage):
    """Fired when /planning/status delivers a new event."""

    def __init__(self, status: PlannerStatus) -> None:
        super().__init__()
        self.status = status


class InspectLine(TUIMessage):
    """Fired when a message arrives that should be shown in the inspect pane."""

    def __init__(self, line: str) -> None:
        super().__init__()
        self.line = line


# ---------------------------------------------------------------------------
# Conversation message widget
# ---------------------------------------------------------------------------


class ChatMessage(Static):
    """A single message bubble in the conversation area."""

    DEFAULT_CSS = """
    ChatMessage {
        padding: 0 1;
        margin: 0 0 1 0;
    }
    ChatMessage.user {
        color: $text;
    }
    ChatMessage.assistant {
        color: $success;
    }
    ChatMessage.system {
        color: $warning;
        text-style: dim;
    }
    ChatMessage.tool_dispatch {
        color: $text-muted;
        text-style: dim;
        padding: 0 2;
    }
    """

    def __init__(self, role: str, text: str) -> None:
        prefix = {
            "user": "> ",
            "assistant": "",
            "system": "  ",
            "tool_dispatch": "  ↳ ",
        }.get(role, "")
        super().__init__(prefix + text, classes=role)


# ---------------------------------------------------------------------------
# Status bar widget
# ---------------------------------------------------------------------------


class StatusBar(Static):
    """Single-line status between conversation and input. Hidden when idle."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
        text-style: dim;
    }
    StatusBar.hidden {
        display: none;
    }
    """

    status_text: reactive[str] = reactive("")

    def render(self) -> str:
        return self.status_text

    def set_status(self, text: str) -> None:
        if text:
            self.remove_class("hidden")
        else:
            self.add_class("hidden")
        self.status_text = text


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------


class ChatApp(App[None]):
    """Full-screen textual TUI for agentbus chat."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #header-bar {
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }

    #conversation {
        height: 1fr;
        border: none;
        padding: 1 1 0 1;
        overflow-y: scroll;
    }

    #inspect-container {
        height: 8;
        border-top: solid $surface-lighten-2;
        display: none;
    }

    #inspect-container.visible {
        display: block;
    }

    #inspect-log {
        height: 100%;
        scrollbar-size: 1 1;
    }

    #status-bar {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
        text-style: dim;
    }

    #status-bar.hidden {
        display: none;
    }

    #input-area {
        height: 3;
        border-top: solid $surface-lighten-2;
        padding: 0 1;
    }

    #chat-input {
        width: 100%;
    }
    """

    BINDINGS = [
        Binding("ctrl+i", "toggle_inspect", "Inspect", show=True),
        Binding("ctrl+d", "quit_session", "Quit", show=True),
        Binding("escape", "clear_status", "Clear", show=False),
    ]

    def __init__(
        self,
        config: ChatConfig,
        build_bus: Callable[[], tuple[MessageBus, ChatPlannerNode]],
    ) -> None:
        super().__init__()
        self._config = config
        self._build_bus = build_bus
        self._bus: MessageBus | None = None
        self._planner: ChatPlannerNode | None = None
        self._bus_task: asyncio.Task | None = None
        self._response_task: asyncio.Task | None = None
        self._status_task: asyncio.Task | None = None
        self._inspect_pattern: str | None = None
        self._response_queue: asyncio.Queue[OutboundChat] = asyncio.Queue()
        self._status_queue: asyncio.Queue[PlannerStatus] = asyncio.Queue()

    def compose(self) -> ComposeResult:
        model_str = f"{self._config.provider}/{self._config.model}"
        yield Static(
            f"AgentBus v{_AGENTBUS_VERSION} • {model_str}",
            id="header-bar",
        )
        with ScrollableContainer(id="conversation"):
            pass
        with Vertical(id="inspect-container"):
            yield Log(id="inspect-log", highlight=True)
        yield Static("", id="status-bar", classes="hidden")
        with Vertical(id="input-area"):
            yield Input(placeholder="Type a message…", id="chat-input")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        self._bus, self._planner = self._build_bus()
        self._bus_task = asyncio.create_task(self._bus.spin())
        self._response_task = asyncio.create_task(self._poll_responses())
        self._status_task = asyncio.create_task(self._poll_status())
        self.query_one("#chat-input").focus()

    async def on_unmount(self) -> None:
        for task in (
            getattr(self, "_response_task", None),
            getattr(self, "_status_task", None),
        ):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._bus_task:
            self._bus_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._bus_task, timeout=3.0)

    # ── Queue polling ─────────────────────────────────────────────────────────

    async def _poll_responses(self) -> None:
        while True:
            response: OutboundChat = await self._response_queue.get()
            self.post_message(ResponseReceived(response.text))

    async def _poll_status(self) -> None:
        while True:
            status: PlannerStatus = await self._status_queue.get()
            self.post_message(StatusUpdated(status))

    # ── Message handlers ──────────────────────────────────────────────────────

    def on_response_received(self, event: ResponseReceived) -> None:
        self._append_message("assistant", event.text)
        self._set_status("")

    def on_status_updated(self, event: StatusUpdated) -> None:
        status = event.status
        ev = status.event

        # Update status bar
        if ev == "thinking":
            self._set_status("[thinking...]")
        elif ev == "tool_dispatched" and status.tool_name:
            self._set_status(f"[dispatching: {status.tool_name}]")
            # Also show in conversation as dim tool dispatch line
            self._append_message("tool_dispatch", status.tool_name)
            # And in inspect pane if open
            ts = datetime.now().strftime("%H:%M:%S")
            self.post_message(InspectLine(f"[{ts}] tool_dispatched → {status.tool_name}"))
        elif ev == "tool_received" and status.tool_name:
            self._set_status(f"[received: {status.tool_name}]")
            ts = datetime.now().strftime("%H:%M:%S")
            self.post_message(InspectLine(f"[{ts}] tool_received ← {status.tool_name}"))
        elif ev == "responding":
            self._set_status("[responding...]")
        elif ev == "error":
            detail = status.detail or "unknown error"
            self._set_status(f"[error: {detail}]")

    def on_inspect_line(self, event: InspectLine) -> None:
        try:
            log = self.query_one("#inspect-log", Log)
            log.write_line(event.line)
        except NoMatches:
            pass

    # ── Input handling ────────────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()

        if text.startswith("/"):
            await self._handle_slash(text)
            return

        # Show user message
        self._append_message("user", text)
        self._set_status("[thinking...]")

        # Publish to bus
        self._bus.publish(
            "/inbound",
            InboundChat(channel="tui", sender="user", text=text),
        )

    async def _handle_slash(self, text: str) -> None:
        result: CommandResult = await handle_command(
            text,
            bus=self._bus,
            planner=self._planner,
            config=self._config,
        )
        if result.quit:
            await self.action_quit_session()
            return
        if result.inspect_toggle is not None:
            await self.action_toggle_inspect(result.inspect_toggle)
        if result.output is not None:
            self._append_message("system", result.output)
        if result.error:
            self._append_message("system", f"Error: {result.error}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _append_message(self, role: str, text: str) -> None:
        conv = self.query_one("#conversation")
        conv.mount(ChatMessage(role, text))
        conv.scroll_end(animate=False)

    def _set_status(self, text: str) -> None:
        bar = self.query_one("#status-bar")
        if text:
            bar.remove_class("hidden")
        else:
            bar.add_class("hidden")
        bar.update(text)

    # ── Actions ───────────────────────────────────────────────────────────────

    async def action_toggle_inspect(self, pattern: str = "**") -> None:
        container = self.query_one("#inspect-container")
        if container.has_class("visible"):
            container.remove_class("visible")
            self._inspect_pattern = None
        else:
            container.add_class("visible")
            self._inspect_pattern = pattern
            log = self.query_one("#inspect-log", Log)
            log.clear()
            log.write_line(f"[inspect: {pattern}]")

    async def action_quit_session(self) -> None:
        if self._bus_task:
            self._bus_task.cancel()
        await self.exit()

    def action_clear_status(self) -> None:
        self._set_status("")
