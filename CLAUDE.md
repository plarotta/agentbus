# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

See `progress.md` for granular status and `plan.md` for full implementation spec. Phases 1–6 of the MVP are complete. The `agentbus chat` interactive mode (see `feature-intereactive_mode.md`) is also complete — all 4 phases landed, wired into `agentbus/chat/` and reachable via `agentbus chat`.

## What AgentBus Is

A ROS-inspired typed pub/sub message bus for LLM agent orchestration. Local-first, asyncio-native, introspection-first. The core idea: agents communicate through typed topics (pub/sub), not direct function calls. The bus owns routing; the harness owns the LLM loop. Neither knows the other's internals.

## Commands

```bash
uv sync --extra dev          # install with dev deps (use uv, not pip)
uv sync --extra anthropic    # install a specific provider extra
uv sync --extra tui          # install textual for the chat TUI
uv sync --extra all          # install all optional deps
uv run pytest tests/ -v      # run all tests (209 passing)
uv run pytest tests/test_chat.py -v      # single test file
uv run pytest tests/test_chat.py::TestChatSession::test_headless_echo -v  # single test
uv run agentbus chat         # launch interactive chat mode (reads ./agentbus.yaml)
uv run agentbus launch agentbus.yaml     # non-chat YAML launcher
```

`asyncio_mode = "auto"` is set in `pyproject.toml`, so async test functions do not need `@pytest.mark.asyncio`.

## Technology Stack

- **Python 3.12+**, **Pydantic v2**, **asyncio** throughout — no threading, no sync blocking
- Generic models use `TypeVar` + `Generic[T]` (not PEP 695 `class Foo[T]` syntax) for Pydantic v2 compatibility. No `from __future__ import annotations` in Pydantic model files.
- **uv** for package management
- **Unix domain sockets** for CLI introspection interface

## Package Layout (authoritative — from PRD §9)

Files marked ✓ exist. Others are planned for later phases.

```
agentbus/
├── __init__.py              # public API
├── bus.py                   # MessageBus, _BusHandle
├── topic.py                 # Topic[T], retention buffer
├── node.py                  # Node ABC, NodeHandle, BusHandle protocol
├── message.py               # Message[T] frozen envelope
├── introspection.py         # TopicInfo, NodeInfo, BusGraph, SpinResult (dataclasses only)
├── errors.py                # AgentBusError hierarchy
├── utils.py                 # CircuitBreaker
├── cli.py                   # agentbus CLI — chat, topic, node, graph, launch
├── launch.py                # YAML launcher
├── gateway.py               # GatewayNode base, v2 prep
├── harness/                 # LLM harness — zero imports from agentbus.bus/topic/node
│   ├── loop.py              # Harness main loop
│   ├── session.py           # Session persistence (~/.agentbus/sessions/<id>/main.json)
│   ├── compaction.py        # Context compaction
│   ├── extensions.py        # HarnessDeps, hooks
│   └── providers/           # anthropic.py, openai.py, ollama.py (+ SystemPrompt, ToolSchema)
├── chat/                    # Interactive `agentbus chat` mode (see below)
│   ├── __init__.py          # public API: run_chat, ChatSession, ChatConfig, load_config, first_run_wizard
│   ├── _config.py           # ChatConfig dataclass, load_config, first_run_wizard
│   ├── _runner.py           # ChatSession (bus wiring, headless loop, TUI dispatch)
│   ├── _planner.py          # ChatPlannerNode (wraps Harness, bridges tool calls through the bus)
│   ├── _tools.py            # ChatToolNode + TOOL_SCHEMAS/TOOL_HANDLERS (bash, file_read, file_write, code_exec)
│   ├── _commands.py         # slash-command dispatcher + CommandResult
│   └── _tui.py              # textual-based split-pane TUI (optional — requires `textual`)
├── schemas/
│   ├── system.py            # LifecycleEvent, Heartbeat, BackpressureEvent, TelemetryEvent
│   ├── common.py            # InboundChat, OutboundChat, ToolRequest, ToolResult
│   └── harness.py           # ToolCall, ToolResult, ContentBlock, PlannerStatus, ConversationTurn
└── nodes/
    └── observer.py          # ObserverNode
```

## Key Implementation Patterns

### Message construction
Nodes never construct `Message` objects. They pass raw payloads to `BusHandle.publish()`. The bus builds the full `Message` envelope (setting `id`, `timestamp`, `source_node`, `topic`). The frozen model guarantees `source_node` integrity from creation.

### ToolResult naming
`ToolResult` exists in two places intentionally:
- `schemas/common.py` — bus-facing; published as a message on `/tools/result`
- `schemas/harness.py` — harness-internal; returned by the `tool_executor` callback

They have identical fields. The PlannerNode bridge maps between them. When importing both in the same file, alias one: `from agentbus.schemas.common import ToolResult as BusToolResult`.

### Harness isolation
`agentbus/harness/` has **zero imports** from `agentbus.bus`, `agentbus.topic`, or `agentbus.node`. The only connection is the `tool_executor: Callable` passed to `Harness(...)` at construction time. Tests for the harness layer inject fake deps via `HarnessDeps` — no bus instance needed.

### Built-in `/system` topics
Auto-registered by the bus at `__init__` — nodes never publish to these:
- `/system/lifecycle` — `LifecycleEvent` on node state transitions
- `/system/heartbeat` — `Heartbeat` snapshot every 30s
- `/system/backpressure` — `BackpressureEvent` on queue overflow
- `/system/telemetry` — `TelemetryEvent` from the harness

### Circuit breakers
Every retry loop uses a `CircuitBreaker` from `utils.py`. Named constants per operation:
- Compaction: `MAX_CONSECUTIVE_COMPACT_FAILURES = 3`
- Tool execution: `MAX_CONSECUTIVE_TOOL_FAILURES = 5`
- Provider calls: `MAX_CONSECUTIVE_PROVIDER_FAILURES = 3`
- Node errors: `MAX_CONSECUTIVE_NODE_ERRORS = 10`

### Wildcard topic matching
`Topic.matches(pattern)` supports two wildcards: `*` (exactly one path segment) and `**` (zero or more segments). These work at any position in the pattern, not just the final segment. Implementation: `Topic.matches()` delegates to the module-level `_match_pattern(pattern, name)`, which calls a recursive `_match_parts` helper. `_match_pattern` is also exported and used directly by `bus.py` for publication-side checks (subscriptions use `t.matches(pattern)`, publications use `_match_pattern(p, t.name)`).

### Backpressure policy scope
Backpressure policy is **per-topic**, not per-subscriber. `Topic(..., backpressure_policy="drop-oldest"|"drop-newest")`. The plan's `"block"` policy is not implemented.

### NodeState naming
`NodeState` uses `CREATED`/`STOPPED` (not `INIT`/`SHUTDOWN` as plan spec says). These name the state the node is *in*, not the transition it's *doing*. All four values: `CREATED` (initial), `RUNNING` (after on_init), `STOPPED` (after on_shutdown), `ERROR` (circuit breaker tripped).

### Error hierarchy
All custom exceptions inherit from `AgentBusError` (base class in `errors.py`).

### Backpressure return pattern
`topic.put(msg)` returns `list[BackpressureEvent]` rather than publishing them directly. The bus receives the list and publishes each event to `/system/backpressure`. This keeps Topic free of bus imports.

### BusHandle as Protocol
`BusHandle` is defined as a `@runtime_checkable` `typing.Protocol` in `node.py` so `Node.on_init(bus: BusHandle)` can be type-hinted without importing from `bus.py`. The concrete implementation (`_BusHandle`) lives in `bus.py` and satisfies the protocol structurally. Exposes `publish(topic, payload)`, `request(topic, payload, reply_on, *, timeout)`, and `topic_history(topic, n)`.

### Topic[T] runtime schema binding
`Topic.__class_getitem__` captures the type parameter at class creation time:
```python
class Topic:
    def __class_getitem__(cls, schema_type):
        return type(f"Topic[{schema_type.__name__}]", (cls,), {"_schema": schema_type})

    def __init__(self, name, retention=0, description=""):
        self.schema = self.__class__._schema  # set by __class_getitem__
```
`Topic[ToolRequest]("/tools/request")` → creates a subclass with `_schema = ToolRequest`, then instantiates it.

### Unified NodeHandle queue
Each `NodeHandle` holds a single `asyncio.Queue` (not one per subscribed topic). Default max size is `_DEFAULT_QUEUE_SIZE = 100` (defined in `node.py`). When `topic.put()` fans out, it writes the message to each subscribed node's queue. `msg.topic` in the envelope handles routing in `on_message`. `Topic.add_subscriber(node_name, queue)` receives the queue reference from the NodeHandle.

### concurrency_mode
`Node.concurrency_mode: Literal["parallel", "serial"] = "parallel"`. When `"serial"`, the bus creates `Semaphore(1)` regardless of `node.concurrency`. Use `"serial"` for nodes that mutate filesystem or system state.

### `on_message` signature
`Node.on_message(self, msg: Message)` takes only the message — not `(topic, msg)`. The topic is available as `msg.topic`.

### `_utcnow()` duplication
`_utcnow()` is duplicated in `message.py`, `schemas/system.py`, and `schemas/harness.py`. Known cleanup candidate to move into `utils.py`, not blocking any active phase.

### Introspection methods on MessageBus
All implemented in `bus.py`, not `introspection.py` (data classes only live there):
- `topics() -> list[TopicInfo]` — sync
- `nodes() -> list[NodeInfo]` — sync
- `graph() -> BusGraph` — sync
- `history(topic, n) -> list[Message]` — sync; empty list if topic missing or retention=0
- `wait_for(topic, predicate, timeout) -> Message` — async; raises `RequestTimeoutError` if topic missing or timeout expires
- `echo(topic, n, filter) -> AsyncIterator[Message]` — async generator; adds a temp subscriber, always removes it on exit (including cancellation); uses a 0.5s internal timeout per `get()` to stay cancellable

### Socket server
`MessageBus(socket_path="/tmp/agentbus.sock")` starts a Unix socket server during `spin()`. Pass `socket_path=None` to disable (useful in tests). Protocol: newline-delimited JSON. Commands: `topics`, `nodes`, `node_info`, `graph`, `history`, `echo`. macOS AF_UNIX paths are limited to 104 characters — tests must use short paths (e.g. `tempfile.mkdtemp(dir="/tmp")`), not pytest's `tmp_path`.

### Test isolation by phase
Phase 1–2 tests run without a `MessageBus` instance. `Topic` fan-out tests pass `asyncio.Queue()` directly to `add_subscriber()`. `NodeHandle` tests construct handles directly. Phase 3a/3b tests use a real `MessageBus` with `spin_once()` as the primary testing primitive. Socket server tests pass `socket_path=None` on most buses and only test the socket explicitly in `test_introspection.py`.

## Key Invariants

- `source_node` on a `Message` is always set by the bus, never by the node
- Publishing to a topic not in a node's `publications` raises `UndeclaredPublicationError`
- Publishing a payload that doesn't match the topic's schema raises `TopicSchemaError`
- `on_message` exceptions never crash the node — caught, logged, published to `/system/lifecycle`
- The harness has zero imports from the bus layer — bridge is always via callback
- `spin_once()` is the primary testing primitive

## CLI

```bash
agentbus chat [--config agentbus.yaml] [--provider ...] [--model ...] \
              [--session ID] [--no-memory] [--verbose|--quiet] [--headless]
agentbus topic list
agentbus topic echo /tools/request
agentbus node list
agentbus node info planner
agentbus graph --format mermaid        # json | mermaid | dot
agentbus launch agentbus.yaml
```

`topic`/`node`/`graph` connect to a running bus via the Unix socket at `/tmp/agentbus.sock`. `chat` is self-contained — it builds its own bus in-process.

## Chat mode architecture

`agentbus chat` wires up a full bus session for an interactive LLM conversation. Entry: `cli.py::_run_chat` → `chat._runner.run_chat` → `ChatSession.run`.

- **Config discovery**: `agentbus.yaml` in the CWD. If absent, `first_run_wizard` (in `chat/_config.py`) prompts and writes one. The wizard uses `input()` only — no external TUI deps required to bootstrap.
- **ChatConfig fields**: `provider` (ollama/mlx/anthropic/openai), `model`, `tools` (list of tool names), `memory` (bool). CLI flags `--provider/--model/--no-memory` override loaded config.
- **Provider validation is eager**: `_planner._make_provider` runs *before* bus setup and raises `SystemExit` with an install hint if the provider's optional package (e.g. `anthropic`, `openai`, `httpx`) is missing. Fail-fast, never mid-conversation. Do not defer this check into `on_init`.
- **Topics registered by ChatSession** (not auto-registered by the bus): `/inbound` (InboundChat, retention 50), `/outbound` (OutboundChat, retention 50), `/tools/request` (ToolRequest, retention 20), `/tools/result` (ToolResult, retention 20), `/planning/status` (PlannerStatus, retention 20).
- **Nodes**: `ChatPlannerNode` (subscribes `/inbound`, publishes `/outbound`, `/tools/request`, `/planning/status`; `concurrency_mode="serial"`), `ChatToolNode` (subscribes `/tools/request`, publishes `/tools/result`), `_ChatCaptureNode` (read-only bridge: `/outbound` and `/planning/status` → asyncio Queues the runner awaits).
- **Tool-call bridge**: the planner's `tool_executor` callback calls `bus.request("/tools/request", ..., reply_on="/tools/result", timeout=60)` — this is the ONLY path from harness → bus. `ChatToolNode.on_message` executes the handler and publishes `BusToolResult` with `correlation_id=msg.correlation_id` so the request future resolves. `HarnessToolResult` and `BusToolResult` are mapped explicitly in `tool_executor`.
- **Known-benign log filter**: `_ChatBusFilter` is installed on `agentbus.bus` logger during `ChatSession.run()` to suppress "no publishers" (for `/inbound`, which the runner publishes directly) and "no subscribers" (for `/tools/result`, which uses pending futures). Removed in `finally`.
- **I/O modes**, selected at `_run_inner`: headless stdin/stdout (always available), verbose headless (prints `↳ tool_name` lines from `/planning/status`), and textual TUI (only when stdout is a TTY AND `textual` is importable AND `--headless` is not set). TUI lives in `chat/_tui.py`; test files mock it out.
- **Session persistence**: `Session` objects at `~/.agentbus/sessions/<uuid>/main.json`. Resume with `agentbus chat --session <id>`. `_cmd_session_list` reads from `DEFAULT_SESSION_ROOT` in `harness/session.py`.
- **Slash commands**: parsed by `chat/_commands.py::handle_command`. Returns a `CommandResult` with fields `output`, `quit`, `inspect_toggle` (TUI-only signal), `error`. New commands should return `CommandResult`, never print directly — the runner/TUI owns the output surface.
- **Tool definitions** live in `chat/_tools.py::TOOL_SCHEMAS` (LLM-facing JSON schema) and `TOOL_HANDLERS` (async handlers). Adding a tool requires an entry in both dicts plus listing its name in `ChatConfig.tools`.
