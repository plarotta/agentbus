"""ChatSession — wires up the bus, nodes, and I/O for agentbus chat."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
import sys
from typing import TYPE_CHECKING

from agentbus.bus import MessageBus
from agentbus.chat._commands import CommandResult, handle_command
from agentbus.chat._config import ChatConfig
from agentbus.chat._planner import ChatPlannerNode
from agentbus.chat._tools import ChatToolNode
from agentbus.harness.session import Session
from agentbus.mcp import MCPGatewayNode, MCPRuntime, open_mcp_runtime
from agentbus.memory import (
    MEMORY_SEARCH_SCHEMA,
    MemoryNode,
    MemoryRuntime,
    build_embedding_provider,
    open_memory_runtime,
)
from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest
from agentbus.schemas.common import ToolResult as BusToolResult
from agentbus.schemas.harness import PlannerStatus
from agentbus.topic import Topic


class _ChatBusFilter(logging.Filter):
    """Suppress expected validation warnings that are benign in chat mode.

    /inbound has no declared publishers (the runner publishes directly).
    /tools/result has no subscribers (request/reply uses pending futures).
    Applied only for the duration of a ChatSession.run() call.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "no publishers" not in msg and "no subscribers" not in msg


if TYPE_CHECKING:
    pass

_AGENTBUS_VERSION = "0.1.0"

# ANSI codes
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"


def _is_terminal() -> bool:
    return sys.stdout.isatty()


def _truncate_repr(value, limit: int = 60) -> str:
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _terminal_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


# ---------------------------------------------------------------------------
# Internal capture node — bridges bus messages to asyncio Queues
# ---------------------------------------------------------------------------


class _ChatCaptureNode(Node):
    """Read-only node that forwards /outbound and /planning/status to queues."""

    name = "chat_capture"
    subscriptions = ["/outbound", "/planning/status"]
    publications: list[str] = []

    def __init__(
        self,
        response_queue: asyncio.Queue,
        status_queue: asyncio.Queue,
    ) -> None:
        self._responses = response_queue
        self._statuses = status_queue

    async def on_message(self, msg: Message) -> None:
        if msg.topic == "/outbound":
            await self._responses.put(msg.payload)
        elif msg.topic == "/planning/status":
            await self._statuses.put(msg.payload)


# ---------------------------------------------------------------------------
# ChatSession
# ---------------------------------------------------------------------------


class ChatSession:
    """Manages a full agentbus chat session: bus, nodes, and I/O.

    Three I/O modes:
      * headless — plain stdin/stdout (scripts, CI, piped input)
      * headless + verbose — headless with inline tool-dispatch lines
      * tui — prompt_toolkit + rich (requires `uv sync --extra tui`)
    """

    def __init__(
        self,
        config: ChatConfig,
        *,
        headless: bool = False,
        verbose: bool | None = None,
        session_id: str | None = None,
        socket_path: str | None = None,
    ) -> None:
        self._config = config
        self._headless = headless
        # Auto-enable verbose if terminal is wide enough
        if verbose is None:
            verbose = _is_terminal() and _terminal_width() >= 100
        self._verbose = verbose
        self._session_id = session_id
        self._socket_path = socket_path or "/tmp/agentbus.sock"

        self._bus: MessageBus | None = None
        self._planner: ChatPlannerNode | None = None
        self._session: Session | None = None
        self._mcp_runtime: MCPRuntime | None = None
        self._memory_runtime: MemoryRuntime | None = None
        self._response_queue: asyncio.Queue[OutboundChat] = asyncio.Queue()
        self._status_queue: asyncio.Queue[PlannerStatus] = asyncio.Queue()

    # ── Bus construction ─────────────────────────────────────────────────────

    def _build_bus(self) -> MessageBus:
        bus = MessageBus(socket_path=self._socket_path)

        # Register chat topics
        bus.register_topic(Topic[InboundChat]("/inbound", retention=50))
        bus.register_topic(Topic[OutboundChat]("/outbound", retention=50))
        bus.register_topic(Topic[ToolRequest]("/tools/request", retention=20))
        bus.register_topic(Topic[BusToolResult]("/tools/result", retention=20))
        bus.register_topic(Topic[PlannerStatus]("/planning/status", retention=20))

        # Load or create session
        session: Session
        if self._session_id:
            try:
                session = Session.load(self._session_id)
            except Exception:
                print(
                    f"[warn] Could not load session {self._session_id!r}, starting fresh.",
                    file=sys.stderr,
                )
                session = Session()
        else:
            session = Session()
        self._session = session

        # Register nodes
        extra_tools = []
        if self._mcp_runtime is not None:
            extra_tools.extend(self._mcp_runtime.tool_schemas())
        if self._memory_runtime is not None:
            extra_tools.append(MEMORY_SEARCH_SCHEMA)

        self._planner = ChatPlannerNode(self._config, session, extra_tools=extra_tools)
        bus.register_node(self._planner)

        if self._config.tools:
            bus.register_node(
                ChatToolNode(
                    self._config.tools,
                    permissions=self._config.permissions,
                    approval_callback=self._make_approval_callback(),
                )
            )

        if self._mcp_runtime is not None:
            bus.register_node(MCPGatewayNode(self._mcp_runtime))

        if self._memory_runtime is not None:
            bus.register_node(MemoryNode(self._memory_runtime))

        bus.register_node(
            _ChatCaptureNode(
                response_queue=self._response_queue,
                status_queue=self._status_queue,
            )
        )

        return bus

    # ── Approval callback ────────────────────────────────────────────────────

    def _make_approval_callback(self):
        """Return a callback suitable for the active I/O mode.

        TTY (headless or TUI): prompt the user via stdin, fail closed on EOF.
            Under the prompt_toolkit TUI, ``patch_stdout`` lets the approval
            prompt surface between turns without corrupting the input line.
        Non-TTY (piped input, tests): deny everything — the planner sees a
            permission-denied result and moves on.
        """
        if not _is_terminal():
            return None

        async def _prompt(tool: str, params: dict, reason: str) -> bool:
            import sys

            summary = ", ".join(f"{k}={_truncate_repr(v)}" for k, v in params.items())
            sys.stdout.write(
                f"\n[approval required] {tool}({summary})\n  reason: {reason}\n  approve? [y/N]: "
            )
            sys.stdout.flush()
            loop = asyncio.get_running_loop()
            try:
                answer = await loop.run_in_executor(None, sys.stdin.readline)
            except (EOFError, KeyboardInterrupt):
                return False
            return answer.strip().lower() in ("y", "yes")

        return _prompt

    # ── Status rendering ─────────────────────────────────────────────────────

    def _render_status_line(self, status: PlannerStatus) -> str:
        """Format a PlannerStatus event as a terminal status string."""
        ev = status.event
        if ev == "thinking":
            return "[thinking...]"
        if ev == "tool_dispatched" and status.tool_name:
            return f"[dispatching: {status.tool_name}]"
        if ev == "tool_received" and status.tool_name:
            return f"[received: {status.tool_name}]"
        if ev == "compacting":
            return "[compacting context...]"
        if ev == "responding":
            return "[responding...]"
        if ev == "error":
            detail = status.detail or "unknown"
            return f"[error: {detail}]"
        return ""

    def _render_verbose_dispatch(self, status: PlannerStatus) -> str:
        """Format a tool_dispatched status as a verbose inline dispatch line."""
        if status.event == "tool_dispatched" and status.tool_name:
            return f"{_DIM}  ↳ {status.tool_name}{_RESET}"
        return ""

    # ── Session summary ──────────────────────────────────────────────────────

    def _print_session_summary(self) -> None:
        if self._session is None:
            return
        turns = len(self._session.turns)
        tokens = self._session.total_tokens()
        sid = self._session.session_id[:8]
        print(f"\nSession {sid}… saved ({turns} turns, {tokens:,} tokens)")
        print(f"Resume with: agentbus chat --session {self._session.session_id}")

    # ── Wait for response ────────────────────────────────────────────────────

    async def _wait_for_response(self) -> OutboundChat:
        """Wait for a response from the planner, showing status if verbose."""
        if not self._verbose or not _is_terminal():
            return await asyncio.wait_for(self._response_queue.get(), timeout=300.0)

        # Verbose mode: show tool dispatches inline as status events arrive
        status_lines_printed = 0

        async def drain_status() -> None:
            nonlocal status_lines_printed
            while True:
                try:
                    status: PlannerStatus = await asyncio.wait_for(
                        self._status_queue.get(), timeout=0.2
                    )
                except TimeoutError:
                    continue
                line = self._render_verbose_dispatch(status)
                if line:
                    print(line)
                    status_lines_printed += 1

        status_task = asyncio.create_task(drain_status())
        try:
            response = await asyncio.wait_for(self._response_queue.get(), timeout=300.0)
        finally:
            status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await status_task
            # Drain remaining status events
            while not self._status_queue.empty():
                try:
                    self._status_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        if status_lines_printed:
            print()
        return response

    # ── Headless loop ────────────────────────────────────────────────────────

    async def _run_headless_loop(self) -> None:
        loop = asyncio.get_event_loop()

        while True:
            # Print prompt
            if _is_terminal():
                sys.stdout.write("> ")
                sys.stdout.flush()

            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except (EOFError, OSError):
                break

            if not line:  # EOF
                break

            text = line.rstrip("\n")
            if not text:
                continue

            # Slash command
            if text.startswith("/"):
                result: CommandResult = await handle_command(
                    text,
                    bus=self._bus,
                    planner=self._planner,
                    config=self._config,
                )
                if result.quit:
                    break
                if result.output is not None:
                    print(result.output)
                if result.error:
                    print(f"Error: {result.error}", file=sys.stderr)
                if result.inspect_toggle is not None:
                    print("[inspect pane requires TUI mode — run without --headless]")
                continue

            # Regular message — publish to bus
            self._bus.publish(
                "/inbound",
                InboundChat(channel="headless", sender="user", text=text),
            )

            # Await response
            try:
                response = await self._wait_for_response()
            except TimeoutError:
                print("\n[error: response timed out after 5 minutes]")
                continue

            print(f"\n{response.text}\n")

    # ── TUI ──────────────────────────────────────────────────────────────────

    async def _run_tui(self) -> None:
        from ._tui import run_tui_app

        assert self._bus is not None and self._planner is not None
        await run_tui_app(
            config=self._config,
            bus=self._bus,
            planner=self._planner,
            response_queue=self._response_queue,
            status_queue=self._status_queue,
        )

    # ── Entry point ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Build the bus, start it, and enter the appropriate I/O loop."""
        _bus_logger = logging.getLogger("agentbus.bus")
        _chat_filter = _ChatBusFilter()
        _bus_logger.addFilter(_chat_filter)

        try:
            await self._run_inner()
        finally:
            _bus_logger.removeFilter(_chat_filter)

    async def _run_inner(self) -> None:
        # Eagerly validate provider dependencies before any bus setup or output.
        # _make_provider raises SystemExit with a clear install message if the
        # required package is missing — fail fast, not on the first user message.
        from ._planner import _make_provider

        _make_provider(self._config)  # raises SystemExit immediately if deps missing

        # Open MCP subprocesses in THIS task so the anyio cancel scopes inside
        # the SDK are entered and exited in the same task (the runtime's
        # aclose() happens in the finally below, also in this task).
        if self._config.mcp_servers:
            try:
                self._mcp_runtime = await open_mcp_runtime(self._config.mcp_servers)
            except SystemExit:
                raise
            except Exception as exc:
                print(f"[warn] MCP setup failed: {exc}", file=sys.stderr)
                self._mcp_runtime = None

        if self._config.memory and self._config.memory_settings.get("enabled"):
            try:
                embeddings = build_embedding_provider(self._config.memory_settings)
                from pathlib import Path as _Path

                self._memory_runtime = await open_memory_runtime(
                    session_id=(self._session.session_id if self._session else "unknown"),
                    db_path=_Path(self._config.memory_settings["db_path"]),
                    embeddings=embeddings,
                )
            except Exception as exc:
                print(
                    f"[warn] Memory setup failed, continuing without memory: {exc}",
                    file=sys.stderr,
                )
                self._memory_runtime = None

        self._bus = self._build_bus()

        use_tui = (
            not self._headless
            and _is_terminal()
            and importlib.util.find_spec("prompt_toolkit") is not None
            and importlib.util.find_spec("rich") is not None
        )

        # Headless mode prints its own banner; TUI mode prints a richer one
        # from inside the prompt_toolkit app. Both share the setup wizard's
        # block-art logo via agentbus.setup.theme so the two entry points
        # feel consistent.
        if not use_tui and _is_terminal():
            from agentbus.setup import theme

            tool_count = len(self._config.tools)
            tool_word = "tool" if tool_count == 1 else "tools"
            print(
                theme.render_banner(
                    _AGENTBUS_VERSION,
                    tagline=(
                        f"chat · {self._config.provider}/{self._config.model}"
                        f" · {tool_count} {tool_word}"
                    ),
                )
            )
            print("  " + theme.colorize("· type /help for commands", theme.MUTED))
            print()

        # Both modes run the bus as a background task so publish/wait_for
        # cycles in the input loop complete against a spinning bus.
        bus_task = asyncio.create_task(self._bus.spin())

        try:
            if use_tui:
                try:
                    await self._run_tui()
                except ImportError:
                    print(
                        "[warn] TUI failed to start, falling back to headless mode.",
                        file=sys.stderr,
                    )
                    await self._run_headless_loop()
            else:
                await self._run_headless_loop()

        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            bus_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(bus_task, timeout=5.0)
            if self._mcp_runtime is not None:
                with contextlib.suppress(Exception):
                    await self._mcp_runtime.aclose()
                self._mcp_runtime = None
            if self._memory_runtime is not None:
                with contextlib.suppress(Exception):
                    self._memory_runtime.close()
                self._memory_runtime = None
            if _is_terminal():
                self._print_session_summary()


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


async def run_chat(
    config: ChatConfig,
    *,
    headless: bool = False,
    verbose: bool | None = None,
    session_id: str | None = None,
    socket_path: str | None = None,
) -> None:
    """Convenience wrapper — creates a ChatSession and runs it."""
    session = ChatSession(
        config,
        headless=headless,
        verbose=verbose,
        session_id=session_id,
        socket_path=socket_path,
    )
    await session.run()
