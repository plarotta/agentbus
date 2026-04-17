"""Slash command parser and handlers for agentbus chat."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentbus.bus import MessageBus
    from agentbus.chat._planner import ChatPlannerNode
    from agentbus.harness.session import Session

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    output: str | None = None  # text to print to the user
    quit: bool = False  # should the session exit?
    inspect_toggle: str | None = None  # topic pattern to toggle inspect (TUI only)
    error: str | None = None  # error message


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_CYAN = "\033[36m"


def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a plain-text table with column padding."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    sep = "─" * (sum(widths) + len(widths) * 3 + 1)
    lines = [sep]
    header_line = "│ " + " │ ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " │"
    lines.append(header_line)
    lines.append(sep)
    for row in rows:
        lines.append("│ " + " │ ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) + " │")
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def _cmd_topics(bus: MessageBus) -> str:
    infos = bus.topics()
    if not infos:
        return "No topics registered."
    rows = [
        [
            t.name,
            t.schema_name,
            str(t.subscriber_count),
            str(t.message_count),
            str(t.retention),
        ]
        for t in infos
    ]
    return _fmt_table(["topic", "schema", "subs", "msgs", "retention"], rows)


async def _cmd_nodes(bus: MessageBus) -> str:
    infos = bus.nodes()
    if not infos:
        return "No nodes registered."
    rows = [
        [
            n.name,
            n.state,
            n.concurrency_mode,
            str(n.messages_received),
            str(n.errors),
        ]
        for n in infos
    ]
    return _fmt_table(["node", "state", "mode", "msgs", "errors"], rows)


async def _cmd_graph(bus: MessageBus) -> str:
    g = bus.graph()
    lines = ["graph TD"]
    for edge in g.edges:
        if edge.direction == "pub":
            lines.append(f'  "{edge.node}" --> "{edge.topic}"')
        else:
            lines.append(f'  "{edge.topic}" --> "{edge.node}"')
    return "\n".join(lines)


async def _cmd_echo(bus: MessageBus, topic: str, n: int = 5) -> str:
    msgs = bus.history(topic, n)
    if not msgs:
        return f"No retained messages on {topic!r}."
    parts = []
    for m in msgs:
        ts = m.timestamp.strftime("%H:%M:%S")
        payload_str = json.dumps(m.payload.model_dump(), default=str)
        if len(payload_str) > 120:
            payload_str = payload_str[:117] + "..."
        parts.append(f"[{ts}] {m.source_node} → {m.topic}\n  {payload_str}")
    return "\n".join(parts)


async def _cmd_history(bus: MessageBus, n: int = 10) -> str:
    msgs = bus._message_log  # access internal log
    recent = list(msgs)[-n:]
    if not recent:
        return "No messages in history."
    parts = []
    for m in recent:
        ts = m.timestamp.strftime("%H:%M:%S")
        parts.append(f"[{ts}] {m.source_node} → {m.topic}")
    return "\n".join(parts)


async def _cmd_session(session: Session) -> str:
    turns = len(session.turns)
    tokens = session.total_tokens()
    lines = [
        f"Session ID:  {session.session_id}",
        f"Turns:       {turns}",
        f"Tokens:      {tokens}",
        f"File:        {session.file_path}",
    ]
    return "\n".join(lines)


async def _cmd_session_list() -> str:
    from agentbus.harness.session import DEFAULT_SESSION_ROOT

    root = DEFAULT_SESSION_ROOT
    if not root.exists():
        return "No sessions found."
    sessions = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        return "No sessions found."
    rows = []
    for s in sessions[:20]:
        mtime = s.stat().st_mtime
        import datetime

        ts = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        rows.append([s.name, ts])
    return _fmt_table(["session_id", "last_modified"], rows)


async def _cmd_tools(config_tools: list[str]) -> str:
    from ._tools import TOOL_SCHEMAS

    rows = []
    for name in config_tools:
        schema = TOOL_SCHEMAS.get(name)
        desc = schema.description[:60] if schema else "unknown"
        rows.append([name, "✓ active", desc])
    return _fmt_table(["tool", "status", "description"], rows)


async def _cmd_provider(config) -> str:
    lines = [
        f"Provider: {config.provider}",
        f"Model:    {config.model}",
    ]
    return "\n".join(lines)


async def _cmd_trace(bus: MessageBus, query: str | None = None, limit: int = 20) -> str:
    """Show the causal chain of messages for a given correlation_id.

    Arguments:
      - None: resolve to the most recent correlation_id in the message log.
      - A correlation_id (prefix match >= 4 chars): trace that chain.
      - A topic name: find the most recent message on that topic with a
        correlation_id, then trace its chain.
    """
    log = list(bus._message_log)
    if not log:
        return "No messages in the bus log yet."

    target_cid: str | None = None

    if query is None:
        for m in reversed(log):
            if m.correlation_id:
                target_cid = m.correlation_id
                break
        if target_cid is None:
            return "No correlation IDs in the bus log yet."
    elif query.startswith("/"):
        # Topic-style argument: find the most recent correlated message on it.
        for m in reversed(log):
            if m.topic == query and m.correlation_id:
                target_cid = m.correlation_id
                break
        if target_cid is None:
            return f"No correlated messages found on topic {query!r}."
    else:
        matches = [m for m in log if m.correlation_id and m.correlation_id.startswith(query)]
        if not matches:
            return f"No messages match correlation_id prefix {query!r}."
        target_cid = matches[-1].correlation_id

    chain = [m for m in log if m.correlation_id == target_cid]
    if not chain:
        return f"Correlation ID {target_cid!r} had no retained messages."

    chain = chain[-limit:]
    header = f"trace {target_cid[:8]}…  ({len(chain)} message(s))"
    parts = [header, "─" * len(header)]
    first_ts = chain[0].timestamp
    for m in chain:
        delta_ms = int((m.timestamp - first_ts).total_seconds() * 1000)
        payload_str = json.dumps(m.payload.model_dump(), default=str)
        if len(payload_str) > 100:
            payload_str = payload_str[:97] + "..."
        parts.append(f"+{delta_ms:>5d}ms  {m.source_node:<16} → {m.topic}")
        parts.append(f"           {payload_str}")
    return "\n".join(parts)


async def _cmd_usage(planner: ChatPlannerNode, config: Any) -> str:
    """Token usage breakdown for the current session."""
    session = planner.session
    turns = session.turns
    if not turns:
        return "No turns recorded in this session yet."

    by_role: dict[str, int] = {}
    by_role_count: dict[str, int] = {}
    for t in turns:
        by_role[t.role] = by_role.get(t.role, 0) + t.token_count
        by_role_count[t.role] = by_role_count.get(t.role, 0) + 1

    total = session.total_tokens()
    provider = getattr(config, "provider", "?")
    model = getattr(config, "model", "?")

    header = [
        f"Provider:    {provider}",
        f"Model:       {model}",
        f"Session:     {session.session_id}",
        f"Total turns: {len(turns)}",
        f"Total tokens: {total}",
    ]

    rows = [
        [role, str(by_role_count[role]), str(by_role[role])]
        for role in sorted(by_role.keys())
    ]
    table = _fmt_table(["role", "turns", "tokens"], rows)
    return "\n".join(header) + "\n\n" + table


async def _cmd_breakers(bus: MessageBus) -> str:
    rows = []
    for name, handle in bus._nodes.items():
        breaker = handle.error_breaker
        state = "open" if breaker.is_open else "closed"
        rows.append([name, state, str(breaker.consecutive_failures)])
    if not rows:
        return "No nodes."
    return _fmt_table(["node", "breaker", "consecutive_failures"], rows)


HELP_TEXT = """\
Introspection:
  /inspect [topic_pattern]  Toggle inspect pane (TUI only). Optional topic filter.
  /topics                   List all topics with stats.
  /nodes                    List all nodes with state and error counts.
  /graph                    Print mermaid topology diagram.
  /echo <topic> [n]         Print last N messages from a topic (default: 5).
  /history [n]              Print last N messages across all topics (default: 10).
  /trace [cid|topic] [n]    Show the causal chain for a correlation_id, or the
                            most recent correlated chain touching a topic.

Session:
  /session                  Show current session ID, turns, token count.
  /session list             List all saved sessions.
  /session fork             Fork the current session at this point.
  /compact                  Force context compaction.
  /usage                    Token usage breakdown by role.

Control:
  /provider                 Show active provider and model.
  /tools                    List available tools with status.
  /clear                    Clear the screen.
  /help                     Print this reference.
  /quit  or  Ctrl-D         Save session and exit.

Debugging:
  /replay <topic> <msg_id>  Re-publish a message from the retention buffer.
  /pause                    Pause all node processing.
  /resume                   Resume processing.
  /breakers                 Show circuit breaker states.\
"""


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


async def handle_command(
    text: str,
    *,
    bus: MessageBus,
    planner: ChatPlannerNode,
    config: Any = None,
) -> CommandResult:
    """Parse and execute a slash command. Returns a CommandResult."""
    text = text.strip()
    if not text.startswith("/"):
        return CommandResult(error="Not a command")

    try:
        parts = shlex.split(text[1:])  # strip leading /
    except ValueError as exc:
        return CommandResult(error=f"Parse error: {exc}")

    if not parts:
        return CommandResult(error="Empty command")

    cmd = parts[0].lower()
    args = parts[1:]
    session = planner.session

    # ── introspection ────────────────────────────────────────────────────────
    if cmd == "topics":
        return CommandResult(output=await _cmd_topics(bus))

    if cmd == "nodes":
        return CommandResult(output=await _cmd_nodes(bus))

    if cmd == "graph":
        return CommandResult(output=await _cmd_graph(bus))

    if cmd == "echo":
        if not args:
            return CommandResult(error="Usage: /echo <topic> [n]")
        topic = args[0]
        n = int(args[1]) if len(args) > 1 else 5
        return CommandResult(output=await _cmd_echo(bus, topic, n))

    if cmd == "history":
        n = int(args[0]) if args else 10
        return CommandResult(output=await _cmd_history(bus, n))

    if cmd == "trace":
        query = args[0] if args else None
        limit = int(args[1]) if len(args) > 1 else 20
        return CommandResult(output=await _cmd_trace(bus, query, limit))

    if cmd == "usage":
        return CommandResult(output=await _cmd_usage(planner, config))

    # ── inspect (TUI-specific, signalled back to caller) ────────────────────
    if cmd == "inspect":
        pattern = args[0] if args else None
        return CommandResult(inspect_toggle=pattern or "**")

    # ── session ──────────────────────────────────────────────────────────────
    if cmd == "session":
        if not args:
            return CommandResult(output=await _cmd_session(session))
        sub = args[0].lower()
        if sub == "list":
            return CommandResult(output=await _cmd_session_list())
        if sub == "fork":
            idx = len(session.turns) - 1
            forked = session.fork(from_turn_index=idx)
            return CommandResult(output=f"Forked session → {forked.file_path}")
        return CommandResult(error=f"Unknown session subcommand: {sub!r}")

    if cmd == "compact":
        # The harness compacts lazily; we trigger it by resetting context tracking
        return CommandResult(
            output="Compaction will trigger automatically on the next turn when needed."
        )

    # ── control ──────────────────────────────────────────────────────────────
    if cmd == "provider":
        return CommandResult(output=await _cmd_provider(config))

    if cmd == "tools":
        tools = config.tools if config else []
        return CommandResult(output=await _cmd_tools(tools))

    if cmd == "clear":
        return CommandResult(output="\033[2J\033[H")  # ANSI clear screen

    if cmd == "help":
        return CommandResult(output=HELP_TEXT)

    if cmd in ("quit", "exit", "q"):
        return CommandResult(quit=True)

    # ── debugging ────────────────────────────────────────────────────────────
    if cmd == "replay":
        if len(args) < 2:
            return CommandResult(error="Usage: /replay <topic> <msg_id>")
        topic, msg_id = args[0], args[1]
        msgs = bus.history(topic, 100)
        match = next((m for m in msgs if m.id == msg_id or m.id.startswith(msg_id)), None)
        if match is None:
            return CommandResult(
                error=f"Message {msg_id!r} not found in {topic!r} retention buffer."
            )
        bus.publish(topic, match.payload)
        return CommandResult(output=f"Re-published {match.id[:8]}… to {topic}")

    if cmd == "pause":
        return CommandResult(
            output="[pause not implemented in this version — use Ctrl-C to interrupt]"
        )

    if cmd == "resume":
        return CommandResult(output="[resume not implemented in this version]")

    if cmd == "breakers":
        return CommandResult(output=await _cmd_breakers(bus))

    return CommandResult(error=f"Unknown command: /{cmd}  (type /help for reference)")
