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
uv sync --extra tui          # install prompt_toolkit + rich + questionary (chat TUI + setup wizard)
uv sync --extra mcp          # install the MCP SDK for mcp_servers: in agentbus.yaml
uv sync --extra slack        # install slack-bolt for channels.slack gateway
uv sync --extra telegram     # install httpx for channels.telegram gateway
uv sync --extra channels     # install both channel extras at once
uv sync --extra all          # install all optional deps
uv run pytest tests/ -v      # run all tests (453 passing)
uv run pytest tests/test_chat.py -v      # single test file
uv run pytest tests/test_chat.py::TestChatSession::test_headless_echo -v  # single test
uv run agentbus chat         # launch interactive chat mode (reads ./agentbus.yaml)
uv run agentbus setup        # interactive setup wizard (writes agentbus.yaml)
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
├── mcp.py                   # MCP gateway — bridges MCP stdio servers to /tools/request
├── swarm.py                 # Hub-and-spoke multi-agent orchestration (SwarmAgentNode, SwarmCoordinatorNode)
├── daemon.py                # agentbus daemon (pidfile, systemd/launchd templates)
├── logging_config.py        # setup_logging(), JSONFormatter, correlation IDs
├── doctor.py                # agentbus doctor diagnostic
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
│   └── _tui.py              # prompt_toolkit + rich interactive shell (optional — requires `prompt_toolkit`, `rich`)
├── channels/                # Multi-channel gateway plugins (Slack, Telegram)
│   ├── __init__.py          # ChannelPlugin, ChannelRuntimeError, load_channels_from_dict, register_plugin
│   ├── base.py              # ChannelPlugin[ConfigT] ABC, MAX_CONSECUTIVE_GATEWAY_FAILURES
│   ├── loader.py            # _REGISTRY, ChannelsRuntime, open_channels_runtime
│   ├── slack/               # SlackPlugin + SlackGatewayNode (slack-bolt Socket Mode)
│   └── telegram/            # TelegramPlugin + TelegramGatewayNode (httpx long-poll)
├── setup/                   # `agentbus setup` interactive wizard (questionary + ANSI theme)
│   ├── __init__.py          # run_setup, Prompter, QuestionaryPrompter, FakePrompter, PromptCancelled
│   ├── theme.py             # palette + banner + section/note renderers (ANSI only)
│   ├── prompter.py          # Protocol + Questionary impl + FakePrompter for tests
│   └── wizard.py            # linear flow: banner → config-detect → provider → tools → memory → channels → doctor
├── schemas/
│   ├── system.py            # LifecycleEvent, Heartbeat, BackpressureEvent, TelemetryEvent, ChannelStatus
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
- `/system/channels` — `ChannelStatus` from multi-channel gateways

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

### Logging
- One logger tree rooted at `agentbus`. Submodules use `logging.getLogger(__name__)`. Nodes access `self.logger` → `agentbus.node.<name>` (child of the root).
- `agentbus.logging_config.setup_logging(level=..., format="text"|"json", stream=...)` is the single entry point. Idempotent — safe to call from tests. Env overrides: `AGENTBUS_LOG_LEVEL`, `AGENTBUS_LOG_FORMAT`, `AGENTBUS_LOG_FILE`. The CLI calls it once in `app()` before dispatching.
- Correlation IDs flow via a `contextvars.ContextVar`. `MessageBus._dispatch_message` calls `set_correlation_id(msg.correlation_id)` before `on_message` and resets it in `finally`. Any `self.logger.info(...)` inside a handler is automatically tagged — no need to pass IDs through helpers. `request_id` / topic tags / etc. can be added via `logger.info("...", extra={"topic": t})` — the JSONFormatter includes any JSON-serializable extras.

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

### Graceful shutdown contract
`MessageBus.spin(drain_timeout=0.0, install_signal_handlers=False)` controls lifecycle exit:
- `drain_timeout` — seconds to let node loops keep pulling queued messages after shutdown is triggered (external timeout, signal, or stop_event). Default 0.0 cancels loops immediately on shutdown — best for tests and bounded runs. Once elapsed, remaining loops are force-cancelled.
- `install_signal_handlers` — when True, SIGTERM/SIGINT trigger cooperative exit via `stop_event`. A **second** signal escalates to immediate cancel (skips the drain window). Default False because library embedders (and the chat TUI) manage their own signals. `agentbus launch` wires `install_signal_handlers=True, drain_timeout=5.0` by default; `bus.shutdown.drain_timeout` / `bus.shutdown.install_signal_handlers` in `agentbus.yaml` override.
- `until` and `max_messages` still exit each node loop immediately (developer-controlled bounds); `drain_timeout` is irrelevant to those paths since the loops have already returned by the time shutdown runs.
- `add_signal_handler` failures (Windows, worker threads) are swallowed — installation is a no-op on those platforms, not a crash.

### Atomic session persistence
`Session.save()` goes through `_atomic_write_text` in `harness/session.py`: write to a sibling tempfile, `fsync`, then `os.replace`. Either the old or the new full JSON is visible at the session path — never a truncated file. Temp files are cleaned up on exception. Safe under SIGKILL mid-write.

### Daemon mode
`agentbus/daemon.py` runs `launch_sync()` in the foreground after taking an `fcntl.flock` advisory lock on the pidfile (default `~/.agentbus/agentbus.pid`). Second-instance launches fail with exit code 2. `daemon stop` reads the pidfile, sends SIGTERM, and polls `is_process_alive(pid)` until the process exits — important caveat: `os.kill(pid, 0)` returns True on **zombie** children, so tests that spawn daemons in-process must reap them (via `Popen.wait`) concurrently for `stop()` to observe the exit. In real usage the service manager is PID 1, so zombies are reaped automatically. `emit_systemd_unit` / `emit_launchd_plist` render `Type=simple` / foreground `ProgramArguments` templates that bake in the absolute path returned by `shutil.which("agentbus")` (fallback: `python -m agentbus`).

### MCP gateway
`agentbus/mcp.py` wraps the official `mcp` Python SDK (optional extra — `uv sync --extra mcp`). Configured MCP servers in `agentbus.yaml` are each spawned as a stdio subprocess; the advertised tool list is discovered once at startup and registered with the planner under `mcp__<server>__<tool>` names. The gateway subscribes to `/tools/request` alongside `ChatToolNode` and silently drops anything it doesn't own — `ChatToolNode` reciprocates by silently dropping non-builtin names, so the two coexist cleanly. `CallToolResult.isError` becomes the bus `error` field; text content blocks are concatenated, image blocks become `[image]`, everything else falls back to `str()`. **Lifecycle ownership is split** by necessity: `open_mcp_runtime()` enters the `stdio_client` / `ClientSession` async context managers in the caller's task; `MCPRuntime.aclose()` must be awaited from the same task at shutdown, because the SDK's underlying anyio cancel scopes cannot cross task boundaries. The chat runner calls `open_mcp_runtime()` at the top of `_run_inner` and `await runtime.aclose()` in the surrounding `finally` — `MCPGatewayNode.on_shutdown` does **not** close the runtime. If MCP setup fails at startup, the runner logs a warning and continues without MCP tools (does not abort the session).

### Memory node
`agentbus/memory.py` ships a `MemoryNode` that consolidates chat history into a searchable local store. Enabled via `memory: { enabled: true, ... }` in `agentbus.yaml`. Architecture:
- **Storage**: SQLite at `~/.agentbus/memory.db` by default. One row per turn: `(id, session_id, ts, user_text, assistant_text, embedding BLOB)`. Embeddings are packed with `struct.pack("{n}f", ...)` for compact float32. Search loads all rows and ranks with a pure-Python `_cosine()` — no numpy. Called **synchronously** from `on_message`, not via `run_in_executor`, because sqlite connections are thread-affine and the executor thread pool violates that. Fine at conversation-turn scale; revisit only if the store grows past ~100k rows.
- **EmbeddingProvider** is a `Protocol` with `dim` and `async embed(texts)`. Default impl is `OllamaEmbeddings` (POSTs to `/api/embed`, requires the `ollama` extra for httpx). Adding a new provider means implementing the protocol and extending `build_embedding_provider()`.
- **Pairing strategy**: planner publishes `/outbound` with a *new* correlation_id (not the inbound's), so `MemoryNode` can't use correlation to match. Instead it holds `_pending_inbound: dict[channel, InboundChat]` and on each `/outbound` does `.popitem()` to pair. Single-channel chat is the only current use case. If multi-channel support lands, pairing must be reworked (track by channel key, or thread correlation_id through the planner).
- **Tool bridge**: `MemoryNode` subscribes to `/tools/request` and replies on `/tools/result`, matching the `MCPGatewayNode` / `ChatToolNode` silent-drop pattern — it only handles `memory_search`. The tool schema is exported as `MEMORY_SEARCH_SCHEMA` and registered with the planner when the runtime is active.
- **Failure modes are fail-closed**: startup embedding probe failure → warning + continue without memory (session still runs). Per-turn embedding failure → log + drop (no partial row). Search embedding failure → returns error in the tool result.
- **Lifecycle**: `open_memory_runtime()` opens the sqlite connection and probes embeddings with `"hello"` before the bus starts. `MemoryRuntime.close()` is called in the runner's `finally`. Same split-ownership model as MCP.

### Multi-channel gateways
`agentbus/channels/` ports the plugin-per-channel pattern from [openclaw](https://github.com/openclaw/openclaw) in a trimmed form. Two channel subpackages ship in-tree: `channels/slack/` (Socket Mode via `slack-bolt`) and `channels/telegram/` (raw `httpx` long-poll). Each subpackage is self-contained and optional — importing `agentbus.channels` does **not** import the SDKs.
- **Plugin contract**: `ChannelPlugin[ConfigT]` (`channels/base.py`) exposes `name: ClassVar[str]`, `ConfigModel: ClassVar[type[BaseModel]]`, `setup_wizard(existing) -> BaseModel`, `create_gateway(config) -> GatewayNode`, and an optional `async probe(config) -> ProbeResult` for `agentbus doctor`. Uses PEP 695 generic syntax — ChannelPlugin is not a Pydantic model, so it sidesteps the Pydantic-v2 `Generic[T]` requirement that message.py/topic.py have.
- **Registry is module-global**. Each plugin subpackage calls `register_plugin(cls)` as an import-time side effect. `load_channels_from_dict` calls `_ensure_plugin_imported(name)` to lazy-import builtin plugin modules so a YAML referring to `slack:` works without the user having to import the subpackage first. Third-party plugins must be imported by the embedder.
- **Config shape**: `channels:` maps plugin name → block; each block is validated against the plugin's `ConfigModel` before the bus starts. `enabled: false` skips a channel; a bare `false` value does the same. Validation errors bubble as `ChannelRuntimeError` with the channel name prefixed.
- **Routing via `OutboundChat.channel`**: the planner echoes `InboundChat.channel` → `OutboundChat.channel`, and the base `GatewayNode` filters `_send_external` by `channel_name` (class attr). `None` channel is accepted by every gateway — legacy single-channel deployments and tests don't need to set it. This keeps Slack from trying to send Telegram replies (and vice versa) when both gateways run on the same bus.
- **Threading context via metadata**: per-channel IDs round-trip in `InboundChat.metadata` → `OutboundChat.metadata`. Slack stores `slack_channel`, `thread_ts`, `ts`; Telegram stores `chat_id`, `message_id`. Gateways read these in `_send_external` to reply in-thread.
- **`/system/channels` topic**: auto-registered by the bus at `__init__` (retention 50). Every gateway publishes `ChannelStatus(channel, state, detail)` on lifecycle transitions — states are `starting | connected | reconnecting | error | stopped`. `publish_channel_status()` is a no-op when `channel_name` is `None`, so legacy gateways don't need to opt in.
- **Reconnect primitives live in `channels/`**, not inside each gateway:
  - `reconnect.py::ReconnectPolicy` — exponential backoff with jitter (defaults: 2s → 30s, factor 1.8, jitter 25%). Each listener loop owns one; `next_delay()` advances, `reset()` on success.
  - Per-channel non-recoverable auth regex — `SLACK_NON_RECOVERABLE_RE` (token_revoked / invalid_auth / missing_scope / …) and `TELEGRAM_NON_RECOVERABLE_RE` (401 / Unauthorized / bot deleted). Matching errors publish `error` state and return immediately — no retry, no breaker decrement. Kept conservative on purpose so transient wording doesn't short-circuit.
  - `CircuitBreaker` with `MAX_CONSECUTIVE_GATEWAY_FAILURES = 5` is still the hard stop for everything else.
- **Outbound chunking**: `channels/chunking.py::chunk_text(text, limit)` splits long replies at `\n\n` / `\n` / ` ` boundaries, hard-chopping only when no boundary exists inside the window. `SLACK_TEXT_LIMIT = 8000`, `TELEGRAM_TEXT_LIMIT = 4096`. Telegram only sets `reply_to_message_id` on the first chunk so follow-ups don't re-quote.
- **Inbound dedup**: `channels/dedup.py::DedupCache` is a capacity-512 LRU keyed on the natural idempotence key (Slack `event["ts"]`, Telegram `update["update_id"]`). Survives Socket Mode redeliveries after reconnect and the race between offset ack and update dispatch.
- **Slack self-echo**: a second `DedupCache` on `SlackGatewayNode._own_ts` records every `ts` returned by `chat.postMessage`. Inbound `_handle_event` drops any event whose `ts` is in that cache so the bot doesn't respond to its own messages in public channels. `_extract_ts(resp)` handles both slack-bolt's `resp.data["ts"]` and plain dict responses used in tests.
- **Stall watchdog**: `channels/watchdog.py::StallWatchdog(idle_s, on_stall)` runs a background task that fires `on_stall` once if `heartbeat()` hasn't been called within `idle_s`. Telegram gateway heartbeats after every successful `_fetch_updates`; `on_stall` cancels the listener so the reconnect loop takes over. Idle threshold = `3 × long_poll_timeout_s + 30s` (30s of silence under normal load is expected). Slack Socket Mode relies on slack-bolt's built-in keepalive — wiring a watchdog around it would duplicate the SDK's own reconnect machinery.
- **Allowlists are mandatory security floor**: both gateways support `allowed_channels` (Slack channel IDs) / `allowed_chats` (Telegram int IDs) and `allowed_senders` (Slack user IDs). Empty list = allow all (appropriate for DM-only bots). Filtering happens *before* the event is published to `/inbound`, so unauthorized messages never reach the planner.
- **Slack specifics**: uses `AsyncSocketModeHandler` — no public HTTPS endpoint required. Requires both an app-level token (`xapp-…`, scope `connections:write`) and a bot token (`xoxb-…`). `message` + `app_mention` events only; subtypes (`channel_join`, deleted, edits) and bot-authored messages are filtered. `_require_slack_sdk()` raises `ChannelRuntimeError` with install instructions if slack-bolt isn't installed.
- **Telegram specifics**: uses httpx (already a transitive dep via `ollama` extra). Long-poll `getUpdates` with `offset` advancement — Telegram's protocol idempotence is driven by the client storing the offset. `long_poll_timeout_s` defaults to 25s (max 60 per Telegram docs). For tests, `TelegramGatewayNode(config, client=fake)` injects a fake httpx-shaped client.
- **CLI**: `agentbus channels list` prints every registered plugin (force-loads builtin ones); `agentbus channels setup <name>` dispatches to the plugin's `setup_wizard` and writes the resulting `model_dump()` back to `channels.<name>` in `agentbus.yaml`.
- **Doctor integration**: `agentbus doctor` runs each enabled plugin's `probe()` asynchronously. `SlackPlugin.probe` calls `auth.test` via `slack_sdk.web.async_client.AsyncWebClient`; `TelegramPlugin.probe` calls `getMe` via a one-shot `httpx.AsyncClient`. Both return `ProbeResult(status="ok|warn|fail", detail=...)`. Missing SDK → `warn` (not `fail`) so the check doesn't hard-block when the optional extra isn't installed.
- **Integration point is `launch` / `daemon`, not `chat`**. Chat mode owns its own stdin/TUI I/O; channel gateways are for long-running deployment. `launch.py::_register_channels` loads the block, swallows per-channel `ChannelRuntimeError`s (logs a warning and continues), and registers surviving gateways as nodes before `spin()`.
- **Deferred on purpose**: multi-account per channel, approvals / interactive replies / typing indicators, Discord / WhatsApp / Signal / iMessage adapters. These were part of openclaw's surface but scope-creep for v1.

### Multi-agent orchestration (swarm)
`agentbus/swarm.py` implements **hub-and-spoke** multi-agent coordination — a coordinator LLM exposes a `dispatch_subagent` tool that routes to named sub-agents on namespaced topics. Sub-agents never talk to each other; handoffs always go through the coordinator. Inspired by claude-code's Task tool.
- **Spec-driven**: `SubAgentSpec(name, description, system_prompt, tools, model)`. `name` becomes the topic-namespace component (`/swarm/<name>/inbound`, `/swarm/<name>/outbound`), so it must be URL-safe. `description` is inlined into the dispatch tool's schema so the coordinator LLM picks the right agent without additional round-trips.
- **One-shot per dispatch**: `SwarmAgentNode.on_message` builds a fresh `Session` + `Harness` every inbound message. Sub-agents are stateless across dispatches — the coordinator owns any long-running conversation. This matches claude-code's Task model: each call is a clean context. (A future "persistent sub-agent" mode would need a per-agent session map + cleanup policy.)
- **`register_swarm(bus, specs, config, *, timeout_s, provider)`**: the single public entry point. Registers the topic pair per spec, instantiates one `SwarmAgentNode` per spec, registers a single `SwarmCoordinatorNode`, and returns the `ToolSchema` the caller passes to its coordinator planner as `extra_tools=[...]`. Must be called *before* `bus.spin()`. `provider` is a test hook — production callers leave it `None`.
- **Correlation-ID preservation is the critical invariant**: `SwarmAgentNode` publishes its `OutboundChat` with `correlation_id=msg.correlation_id`. That is what unblocks the coordinator's `bus.request(...)` future on `/swarm/<name>/outbound`. Without it, dispatches hang until timeout.
- **Silent-drop pattern**: `SwarmCoordinatorNode` subscribes to `/tools/request` alongside `ChatToolNode` / `MCPGatewayNode` / `MemoryNode`; it ignores any request where `tool != "dispatch_subagent"`. Validation errors (unknown agent, empty task) surface as `ToolResult.error`, not exceptions — the coordinator LLM sees them as tool failures and can recover.
- **Provider system prompts**: `_make_swarm_provider` builds a provider per sub-agent with the spec's system prompt. Anthropic gets it via `SystemPrompt(static_prefix=...)`. For providers without a `system_prompt` attribute (ollama/openai in their current form), the prompt is prepended to the task text at dispatch time (gated on `self._prepend_system_prompt`).
- **Tool bridge reuses the existing plumbing**: sub-agent tool calls go through `/tools/request` → `ChatToolNode` (or MCPGateway) just like the coordinator's. The spec's `tools` list filters *what the sub-agent's LLM is told about*; the actual tool node decides what runs. So `tools=["bash"]` on a sub-agent means "the LLM sees bash but nothing else" — permission policies still apply downstream.
- **ClassVar shadowing**: `SwarmAgentNode.__init__` assigns per-instance `name`, `subscriptions`, `publications` (ClassVars on `Node`). Marked `# type: ignore[misc]` — the Node base treats them as class-level but the bus reads them via attribute access, so instance shadowing works at runtime. Same trick in `SwarmCoordinatorNode` for publications.
- **Deferred on purpose**: peer-to-peer sub-agent communication (mesh topology), persistent per-agent sessions, streaming responses (only final-message is returned), nested swarms (a sub-agent spawning its own swarm).

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
agentbus daemon start agentbus.yaml              # pidfile-locked foreground run
agentbus daemon stop                              # SIGTERM + graceful wait
agentbus daemon status                            # running/stale/absent
agentbus daemon install systemd agentbus.yaml   > ~/.config/systemd/user/agentbus.service
agentbus daemon install launchd agentbus.yaml   > ~/Library/LaunchAgents/com.agentbus.daemon.plist
agentbus channels list                             # list registered channel plugins
agentbus channels setup slack                      # legacy per-channel wizard → writes channels.slack into agentbus.yaml
agentbus setup [--config PATH] [--force] [--skip-doctor]  # themed full-config wizard (questionary + ANSI theme)
```

`topic`/`node`/`graph` connect to a running bus via the Unix socket at `/tmp/agentbus.sock`. `chat` is self-contained — it builds its own bus in-process.

## Setup wizard architecture

`agentbus setup` is the polished first-run + reconfigure flow. Entry: `cli.py::app` → `agentbus.setup.run_setup`. Three cooperating modules:

- **`setup/theme.py`** — palette + banner helpers. ANSI-only (no rich dep) so it works in dumb terminals and CI logs. Honors `NO_COLOR` and `AGENTBUS_FORCE_COLOR`. Renders the two-line block-art AgentBus logo, section headers, and tone-tagged notes (`ok/warn/fail/info/muted`).
- **`setup/prompter.py`** — `Prompter` Protocol + two impls. `QuestionaryPrompter` wraps `questionary` (via the `tui` extra) with an AgentBus-cyan style dict; `FakePrompter(answers)` consumes a scripted list so tests run the full flow without a TTY. Cancellation protocol: any Ctrl-C / Ctrl-D / empty script raises `PromptCancelled`, caught once in `run_setup`. This replaces the old scatter of `if answer is None` checks throughout the flow.
- **`setup/wizard.py`** — linear flow: banner → existing-config detection (edit/overwrite/cancel) → provider+model → tools multi-select → memory confirm → channels loop → atomic write → doctor probe → outro. All user-facing strings live in this file, not the prompter, so test assertions can compare on exact wording.

Integration points:
- **`ChannelPlugin.interactive_setup(prompter, existing) -> BaseModel`** is the themed equivalent of the legacy `setup_wizard(existing)`. The base class delegates to `setup_wizard` so unported plugins keep working through the new command. `SlackPlugin` and `TelegramPlugin` override with prompter-driven flows.
- **Atomic write** via `_atomic_write_yaml`: copy existing to `.bak`, write tempfile, `fsync`, `os.replace`. The backup is created *before* the new content is written so a crash mid-rename leaves either old or new visible, never a truncation.
- **Doctor integration**: the final section re-runs the individual `_check_*` functions from `agentbus.doctor` and pipes each result through `prompter.note(...)` with tone-mapped glyphs — the probe output stays visually coherent with the rest of the wizard. Tests pass `run_doctor=False` to skip probes that would touch the network.
- **Exit codes**: `0` (wrote), `1` (cancelled), `2` (channel validation error or questionary missing). Fresh configs with an empty existing-file skip the edit/overwrite/cancel prompt entirely.

## Chat mode architecture

`agentbus chat` wires up a full bus session for an interactive LLM conversation. Entry: `cli.py::_run_chat` → `chat._runner.run_chat` → `ChatSession.run`.

- **Config discovery**: `agentbus.yaml` in the CWD. If absent, `first_run_wizard` (in `chat/_config.py`) prompts and writes one. The wizard uses `input()` only — no external TUI deps required to bootstrap.
- **ChatConfig fields**: `provider` (ollama/mlx/anthropic/openai), `model`, `tools` (list of tool names), `memory` (bool). CLI flags `--provider/--model/--no-memory` override loaded config.
- **Provider validation is eager**: `_planner._make_provider` runs *before* bus setup and raises `SystemExit` with an install hint if the provider's optional package (e.g. `anthropic`, `openai`, `httpx`) is missing. Fail-fast, never mid-conversation. Do not defer this check into `on_init`.
- **Topics registered by ChatSession** (not auto-registered by the bus): `/inbound` (InboundChat, retention 50), `/outbound` (OutboundChat, retention 50), `/tools/request` (ToolRequest, retention 20), `/tools/result` (ToolResult, retention 20), `/planning/status` (PlannerStatus, retention 20).
- **Nodes**: `ChatPlannerNode` (subscribes `/inbound`, publishes `/outbound`, `/tools/request`, `/planning/status`; `concurrency_mode="serial"`), `ChatToolNode` (subscribes `/tools/request`, publishes `/tools/result`), `_ChatCaptureNode` (read-only bridge: `/outbound` and `/planning/status` → asyncio Queues the runner awaits).
- **Tool-call bridge**: the planner's `tool_executor` callback calls `bus.request("/tools/request", ..., reply_on="/tools/result", timeout=60)` — this is the ONLY path from harness → bus. `ChatToolNode.on_message` executes the handler and publishes `BusToolResult` with `correlation_id=msg.correlation_id` so the request future resolves. `HarnessToolResult` and `BusToolResult` are mapped explicitly in `tool_executor`.
- **Known-benign log filter**: `_ChatBusFilter` is installed on `agentbus.bus` logger during `ChatSession.run()` to suppress "no publishers" (for `/inbound`, which the runner publishes directly) and "no subscribers" (for `/tools/result`, which uses pending futures). Removed in `finally`.
- **I/O modes**, selected at `_run_inner`: headless stdin/stdout (always available), verbose headless (prints `↳ tool_name` lines from `/planning/status`), and the prompt_toolkit + rich TUI (only when stdout is a TTY AND both `prompt_toolkit` and `rich` are importable AND `--headless` is not set). TUI lives in `chat/_tui.py` — a non-fullscreen shell (normal scrollback preserved) with persistent input history, a bottom toolbar, inline `↳ tool_name` dim lines, and markdown-rendered responses. Test files mock it out.
- **Session persistence**: `Session` objects at `~/.agentbus/sessions/<uuid>/main.json`. Resume with `agentbus chat --session <id>`. `_cmd_session_list` reads from `DEFAULT_SESSION_ROOT` in `harness/session.py`.
- **Slash commands**: parsed by `chat/_commands.py::handle_command`. Returns a `CommandResult` with fields `output`, `quit`, `inspect_toggle` (TUI-only signal), `error`. New commands should return `CommandResult`, never print directly — the runner/TUI owns the output surface. `/trace` walks `bus._message_log` by `correlation_id`; `/usage` aggregates `session.turns[].token_count` by role.
- **Tool definitions** live in `chat/_tools.py::TOOL_SCHEMAS` (LLM-facing JSON schema) and `TOOL_HANDLERS` (async handlers). Adding a tool requires an entry in both dicts plus listing its name in `ChatConfig.tools`.
- **Tool permissions** live in `chat/_permissions.py`. `PermissionPolicy.check(tool, params)` returns a `PermissionCheck(decision, reason)` where `decision` is one of `"allow" | "deny" | "approval_required"`. Deny rules short-circuit before approval prompts, so `mode: approval_required` + `deny_commands: ["rm"]` on `bash` is safe. File path rules always `expanduser().resolve()` both the target and the rule root before comparison, so `foo/../../etc/passwd` can't escape an `allow_paths` allowlist. `ChatToolNode` takes `permissions=` and `approval_callback=` kwargs; the callback signature is `(tool: str, params: dict, reason: str) -> Awaitable[bool]` and is called only when a check returns `approval_required`. Any exception from the callback fails closed (treated as denial). The stdin-based prompt in `ChatSession._make_approval_callback` only wires up in `headless` mode with `_is_terminal()` — TUI mode passes `None`, so gated tools are denied until a modal dialog is added.
