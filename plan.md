# AgentBus Implementation Plan

## Overview

Build the AgentBus MVP in 7 phases. Each phase produces working, testable code before the next begins. The target is a `pip install agentbus` package under 3,000 LOC for the v1 core.

**Stack:** Python 3.12+, Pydantic v2, asyncio, no external dependencies for the bus core.

**Authoritative package layout (PRD §9):**

```
agentbus/
├── __init__.py              # public API: MessageBus, Topic, Node, Message, Harness
├── bus.py                   # MessageBus implementation
├── topic.py                 # Topic[T], retention buffer
├── node.py                  # Node base class, NodeHandle, BusHandle
├── message.py               # Message[T] envelope
├── introspection.py         # TopicInfo, NodeInfo, BusGraph, SpinResult, metrics
├── errors.py                # all custom exceptions
├── utils.py                 # CircuitBreaker
├── cli.py                   # agentbus CLI (topic, node, graph commands)
├── launch.py                # YAML launcher
├── gateway.py               # GatewayNode base class (v2 prep)
├── harness/
│   ├── __init__.py          # public API: Harness, Extension, Session
│   ├── loop.py              # agent loop: LLM → tool → LLM → respond
│   ├── session.py           # Session, ConversationTurn, branching, persistence
│   ├── compaction.py        # context window summarization
│   ├── extensions.py        # Extension protocol, pipeline runner
│   └── providers/
│       ├── __init__.py      # Provider protocol, Chunk, ToolSchema
│       ├── ollama.py        # Ollama API provider
│       ├── anthropic.py     # Claude API
│       └── openai.py        # OpenAI + compatible endpoints
├── schemas/
│   ├── system.py            # LifecycleEvent, Heartbeat, BackpressureEvent, TelemetryEvent
│   ├── common.py            # InboundChat, OutboundChat, ToolRequest, ToolResult
│   └── harness.py           # ToolCall, ToolResult, PlannerStatus, ConversationTurn
└── nodes/
    └── observer.py          # ObserverNode
```

**Test infrastructure:** `pytest` + `pytest-asyncio`. Tests live in `tests/` mirroring the source layout. Configured in `pyproject.toml` under `[tool.pytest.ini_options]` with `asyncio_mode = "auto"`.

---

## Phase 1 — Foundation (Core data model)

Build the immutable primitives that everything else depends on. No runtime behavior yet — just schemas, envelopes, error types, and project scaffolding.

### Files
- `pyproject.toml` — package metadata, entry points, dependencies, optional extras, pytest config
- `agentbus/__init__.py` — empty for now (populated in Phase 6)
- `agentbus/message.py` — `Message[T]` generic envelope
- `agentbus/errors.py` — all custom exceptions
- `agentbus/utils.py` — `CircuitBreaker`
- `agentbus/schemas/__init__.py`
- `agentbus/schemas/system.py` — `LifecycleEvent`, `Heartbeat`, `BackpressureEvent`, `TelemetryEvent`
- `agentbus/schemas/common.py` — `InboundChat`, `OutboundChat`, `ToolRequest`, `ToolResult`
- `agentbus/schemas/harness.py` — `ToolCall`, `PlannerStatus`, `ConversationTurn`
- `tests/conftest.py`
- `tests/test_message.py`
- `tests/test_schemas.py`

### Spec

**`pyproject.toml`:**
- `[project]` requires `pydantic>=2.0`
- Optional extras:
  - `ollama` = `["httpx"]`
  - `anthropic` = `["anthropic"]`
  - `openai` = `["openai"]`
  - `cli` = `["typer", "pyyaml"]`
  - `all` = all of the above
  - `dev` = `["pytest", "pytest-asyncio"]`
- Entry point: `[project.scripts] agentbus = "agentbus.cli:app"` (requires `cli` extra)

**`Message[T]`:**
- Fields: `id` (uuid4, auto), `timestamp` (utc, auto), `source_node` (str), `topic` (str), `correlation_id` (str | None), `payload` (T)
- Pydantic `model_config = ConfigDict(frozen=True)` — immutable after creation
- **Nodes never construct `Message` objects directly.** Nodes pass raw payloads to `BusHandle.publish()`. The bus constructs the full `Message` envelope, setting `id`, `timestamp`, `source_node`, and `topic`. This is how immutability and `source_node` integrity coexist — the envelope is frozen from the moment of creation, which happens inside the bus, not the node.

**Errors:**
- `TopicSchemaError` — wrong payload type for a topic
- `UndeclaredPublicationError` — node published to topic not in its `publications`
- `UndeclaredSubscriptionError` — node subscribed to unregistered topic
- `DuplicateNodeError` — node name collision at registration
- `DuplicateTopicError` — topic name collision at registration
- `RequestTimeoutError` — request/reply timeout
- `NodeInitError` — `on_init()` raised
- `CircuitBreakerOpenError` — operation rejected because breaker is open

**`CircuitBreaker` (PRD §11.1):**
```python
@dataclass
class CircuitBreaker:
    name: str
    max_failures: int
    consecutive_failures: int = 0

    def record_failure(self) -> bool:  # True if breaker is now open
    def record_success(self): ...
    @property
    def is_open(self) -> bool: ...
```
Used throughout: compaction (max 3), tool execution (max 5), provider calls (max 3), node errors (max 10), backpressure (max 50 dropped/min).

**Schemas — `system.py`:**
- `LifecycleEvent` — node, event (started/stopped/error/init_failed), error, timestamp
- `Heartbeat` — uptime_s, node_count, topic_count, total_messages, messages_per_second, node_states, queue_depths
- `BackpressureEvent` — topic, subscriber_node, queue_size, dropped_message_id, policy
- `TelemetryEvent` (PRD §11.7) — event (stall_detected/context_pressure/tool_timeout/model_demotion/compact_triggered/breaker_tripped), detail, session_id, timestamp

**Schemas — `common.py`:**
- `InboundChat` — channel, sender, text, metadata
- `OutboundChat` — text, reply_to, metadata
- `ToolRequest` — tool, action, params (dict)
- `ToolResult` — tool_call_id, output, error

**Schemas — `harness.py`:**
- `ToolCall` — id, name, arguments (dict)
- `PlannerStatus` — event (thinking/tool_dispatched/tool_received/compacting/responding/error), iteration, context_tokens, context_capacity, tool_name, detail
- `ConversationTurn` — role (user/assistant/tool_result), content, tool_calls, token_count, timestamp

### Tests
- `Message` creation with auto-generated id/timestamp
- `Message` frozen (mutation raises `ValidationError`)
- `source_node` is a required field (no default)
- All schema round-trips (serialize → deserialize → equal)
- `CircuitBreaker`: record N failures → `is_open` becomes True; `record_success` resets

---

## Phase 2 — Topic and Node primitives

Build the `Topic[T]` and `Node` base class. Still no bus runtime — these are registerable units.

### Files
- `agentbus/topic.py` — `Topic[T]`, retention buffer
- `agentbus/node.py` — `Node` (abstract), `NodeHandle`, `BusHandle`, `NodeState`
- `tests/test_topic.py`
- `tests/test_node.py`

### Spec

**`Topic[T]`:**
- `name: str`, `retention: int` (default 0), `description: str` (default "")
- Stores the Pydantic model class for schema validation: `schema: type[BaseModel]`
- Internal `_buffer: deque[Message]` — ring buffer of last N messages (if retention > 0)
- Internal `_queues: dict[str, asyncio.Queue]` — one queue per subscriber node name
- Internal `_backpressure_policy: dict[str, Literal["drop-oldest", "drop-newest", "block"]]` — per-subscriber
- `_metrics: TopicMetrics` — message count, rolling 60s publish rate, last message timestamp, queue depths
- `add_subscriber(node_name, queue_maxsize=100, backpressure="drop-oldest")` — registers a queue
- `put(msg: Message) -> list[BackpressureEvent]` — fan-out to all subscriber queues; returns any backpressure events generated (bus publishes these to `/system/backpressure`)
- `validate_payload(payload) -> None` — checks `isinstance(payload, self.schema)`, raises `TopicSchemaError` if not
- Wildcard matching is a **static function**, not a method — used by the bus to resolve subscriptions: `matches(topic_name: str, pattern: str) -> bool` — prefix match on `/` segments, `*` only valid as final segment

**`Node` (abstract base class):**
- Class-level declarations: `name: str`, `concurrency: int = 1`, `subscriptions: list[str] = []`, `publications: list[str] = []`
- `concurrency_mode: Literal["parallel", "serial"] = "parallel"` (PRD §11.6) — `"serial"` forces `concurrency = 1` regardless of the declared value; bus enforces this. Default `"parallel"` uses the declared `concurrency`. Use `"serial"` for nodes that mutate filesystem/system state.
- Overridable methods (not abstract — default implementations are no-ops so users only implement what they need):
  - `async on_init(self, bus: BusHandle) -> None`
  - `async on_message(self, topic: str, msg: Message) -> None`
  - `async on_shutdown(self) -> None`

**`NodeHandle` (internal, wraps a Node for the bus):**
- Holds: `node: Node`, concurrency `asyncio.Semaphore` (size = 1 if `concurrency_mode == "serial"`, else `node.concurrency`), `state: NodeState`, per-node metrics (messages_received, messages_published, errors, latency_samples: `deque[float]` for mean/p99)
- `NodeState` enum: `INIT`, `RUNNING`, `SHUTDOWN`, `ERROR`

**`BusHandle` (restricted view given to nodes via `on_init`):**
- This is a protocol / interface. The bus provides the concrete implementation. Defined here so `Node.on_init` can type-hint it without importing bus internals.
- Methods:
  - `async publish(topic: str, payload: BaseModel) -> None`
  - `async topic_history(topic: str, n: int = 10) -> list[Message]`
  - `async request(publish_to: str, payload: BaseModel, reply_on: str, timeout: float = 5.0) -> Message`

**Circular import prevention:** `introspection.py` (Phase 3) defines `TopicInfo`, `NodeInfo`, etc. `topic.py` and `node.py` do NOT import from `introspection.py`. The bus assembles `TopicInfo` by reading topic internals — topics expose raw metrics, the bus wraps them.

### Tests
- Topic retention buffer: put N+1 messages with retention=N, oldest evicted
- Topic fan-out: 3 subscribers, publish 1 message, all 3 queues have it
- Topic wildcard: `matches("/tools/request", "/tools/*")` → True; `matches("/memory/query", "/tools/*")` → False
- Topic `validate_payload`: correct type passes, wrong type raises `TopicSchemaError`
- Topic backpressure: fill queue to maxsize with `drop-oldest`, put one more → oldest dequeued, `BackpressureEvent` returned
- `Node` subclass: can declare name/subscriptions/publications, default lifecycle methods are no-ops
- `NodeHandle` semaphore size respects `concurrency_mode = "serial"` (always 1)

---

## Phase 3a — MessageBus core and spin()

The core runtime. Registration, publishing, message routing, and the four-phase `spin()` lifecycle.

### Files
- `agentbus/bus.py` — `MessageBus`
- `agentbus/introspection.py` — `TopicInfo`, `NodeInfo`, `BusGraph`, `SpinResult`
- `tests/test_bus.py`

### Spec

**`MessageBus` construction:**
- Auto-registers system topics at `__init__`: `/system/lifecycle` (`LifecycleEvent`), `/system/heartbeat` (`Heartbeat`), `/system/backpressure` (`BackpressureEvent`), `/system/telemetry` (`TelemetryEvent`)
- Internal state: `_topics: dict[str, Topic]`, `_nodes: dict[str, NodeHandle]`, `_message_log: deque[Message]` (configurable maxlen), `_running: bool`, `_total_messages: int`

**Registration:**
- `register_topic(topic: Topic)` — adds to `_topics`, raises `DuplicateTopicError` on name collision
- `register_node(node: Node)` — validates all subscriptions/publications reference registered topics (expanding wildcards), raises `DuplicateNodeError` / `UndeclaredSubscriptionError` / `UndeclaredPublicationError`; creates `NodeHandle`; calls `topic.add_subscriber()` for each subscription (including wildcard-resolved topics)

**`publish(topic_name: str, payload: BaseModel, source_node: str = "_bus_") -> Message`:**
1. Look up topic → `KeyError` if not found
2. `topic.validate_payload(payload)` → `TopicSchemaError`
3. Construct `Message(id=uuid4, timestamp=utcnow, source_node=source_node, topic=topic_name, payload=payload)`
4. Append to `topic._buffer` (retention)
5. Append to `_message_log`
6. `backpressure_events = topic.put(msg)` (fan-out to subscriber queues)
7. For each backpressure event: `self.publish("/system/backpressure", event, source_node="_bus_")`
8. Increment `_total_messages` and topic metrics
9. Return the `Message`

**Node publish (via BusHandle):**
1. Check `topic_name` is in node's declared `publications` (or matches via wildcard) → `UndeclaredPublicationError`
2. Call `bus.publish(topic_name, payload, source_node=node.name)`

**`spin()` — four phases:**

1. **VALIDATION** — for each node: check subscriptions resolve to registered topics, check publications resolve to registered topics. Detect orphan topics (registered but zero subscribers AND zero publishers among nodes). Detect dead-end nodes (subscribes but never publishes, or vice versa). Log warnings for all issues — never fail validation.

2. **INIT** — `asyncio.gather` all `node.on_init(bus_handle)` with per-node timeout (default 30s). On exception: log, publish `LifecycleEvent(event="init_failed", error=str(e))`, set node state to `ERROR`, skip node (don't crash spin). On success: set state `RUNNING`, publish `LifecycleEvent(event="started")`. Start heartbeat timer task.

3. **SPIN** — for each RUNNING node, launch `asyncio.Task` running `_node_loop(node_handle)`:
   ```
   while self._running:
       msg = await self._next_message(node_handle)  # from any subscribed queue
       async with node_handle.semaphore:
           start = time.monotonic()
           try:
               await node.on_message(msg.topic, msg)
           except Exception as e:
               node_handle.errors += 1
               node_handle.error_breaker.record_failure()
               publish LifecycleEvent(event="error", error=str(e))
               if node_handle.error_breaker.is_open:
                   node_handle.state = ERROR
                   break
           else:
               node_handle.error_breaker.record_success()
           finally:
               record latency sample
               node_handle.messages_received += 1
   ```
   Check termination conditions after each message: `until()` callable, `max_messages` counter, `timeout` wall clock.

4. **SHUTDOWN** — set `_running = False`. Cancel all node loop tasks. Drain queues with 5s timeout. `asyncio.gather` all `node.on_shutdown()` with timeout. Publish `LifecycleEvent(event="stopped")` for each. Cancel heartbeat task. Return `SpinResult`.

**`spin()` signature:**
```python
async def spin(
    self,
    until: Callable[[], bool] | None = None,
    max_messages: int | None = None,
    timeout: float | None = None,
) -> SpinResult
```
Note: `async def` — the caller runs `asyncio.run(bus.spin())` or awaits it. A convenience sync wrapper `spin_sync()` can call `asyncio.run()`.

**`spin_once(timeout: float = 5.0) -> Message | None`:**
- Process exactly one pending message across all nodes
- Returns the message that was processed, or None on timeout
- Primary testing primitive

**Heartbeat timer:** `asyncio.Task` that publishes `Heartbeat(...)` to `/system/heartbeat` every `heartbeat_interval` seconds (default 30). The bus auto-declares itself as publisher of all `/system/*` topics.

**Request/reply (`BusHandle.request`):**
- Generate `correlation_id = uuid4()`
- Create a `asyncio.Future` keyed by `(reply_on_topic, correlation_id)`
- Register a one-shot interceptor on the reply topic: when a message with matching `correlation_id` arrives, resolve the future
- Publish the request with `correlation_id` set
- `await asyncio.wait_for(future, timeout)` → `RequestTimeoutError` on timeout
- Clean up the interceptor regardless of outcome

**`SpinResult`:**
```python
@dataclass
class SpinResult:
    messages_processed: int
    duration_s: float
    per_node: dict[str, NodeStats]  # received, published, errors
    errors: list[str]
```

**`introspection.py` data classes:**
- `TopicInfo` — name, schema, retention, subscriber_count, publisher_count, message_count, rate_hz, last_message_at, queue_depths
- `NodeInfo` — name, state, concurrency, concurrency_mode, active_tasks, subscriptions, publications, messages_received, messages_published, errors, mean_latency_ms, uptime_s
- `BusGraph` — nodes: list[NodeInfo], topics: list[TopicInfo], edges: list[Edge]
- `Edge` — node: str, topic: str, direction: "sub" | "pub"

### Tests
- Register duplicate topic → `DuplicateTopicError`
- Register node with undeclared subscription → `UndeclaredSubscriptionError`
- Register node with undeclared publication → `UndeclaredPublicationError`
- `publish()` with wrong schema → `TopicSchemaError`
- `publish()` sets `source_node` on envelope (nodes can't spoof)
- `spin(max_messages=N)` processes exactly N messages then returns `SpinResult`
- `spin_once()` routes message to correct node, returns it
- `on_message` exception: node stays RUNNING, error count increments, `LifecycleEvent(event="error")` published
- Circuit breaker: 10 consecutive `on_message` errors → node transitions to `ERROR`, loop exits
- Fan-out: 3 subscribers receive same message
- Backpressure: `BackpressureEvent` published to `/system/backpressure` on queue overflow
- Heartbeat published at configured interval
- Request/reply: publish request with correlation_id, matching reply resolves future
- Request/reply timeout: `RequestTimeoutError` raised
- `concurrency_mode="serial"`: two concurrent messages processed sequentially (semaphore=1)
- Validation phase: orphan topic logs warning but spin continues
- INIT phase: one node fails `on_init`, others still start, failed node in ERROR state

---

## Phase 3b — Introspection APIs and socket server

Programmatic introspection and the Unix domain socket interface for the CLI.

### Files
- `agentbus/introspection.py` — add introspection methods and socket server (extends Phase 3a file)
- `tests/test_introspection.py`

### Spec

**Introspection methods on `MessageBus`:**
- `topics() -> list[TopicInfo]` — assembles `TopicInfo` from each topic's internal metrics
- `nodes() -> list[NodeInfo]` — assembles `NodeInfo` from each `NodeHandle`'s state/metrics
- `echo(topic: str, n: int | None = None, filter: Callable | None = None) -> AsyncIterator[Message]` — taps into a topic's subscriber queue (adds a temporary subscriber), yields messages, removes subscriber when done or after `n` messages
- `graph() -> BusGraph` — builds full node-topic connectivity graph from registrations
- `history(topic: str, n: int = 10) -> list[Message]` — returns last N from topic's retention buffer
- `wait_for(topic: str, predicate: Callable[[Message], bool], timeout: float) -> Message` — blocks until matching message, raises `RequestTimeoutError`

**Unix domain socket introspection server:**
- Starts as an `asyncio.Task` during SPIN phase
- Path: configurable, default `/tmp/agentbus.sock`
- Removes stale socket file on startup
- Protocol: newline-delimited JSON. Client sends `{"cmd": "topics"}`, server responds with JSON. For `echo`, server streams one JSON line per message until client disconnects.
- Commands: `topics`, `nodes`, `node_info` (with `name` param), `graph`, `history` (with `topic` and `n` params), `echo` (with `topic` param, streaming)
- Graceful cleanup: remove socket file on shutdown

### Tests
- `topics()` returns all registered topics with correct subscriber counts
- `nodes()` returns all nodes with correct states
- `graph()` edges match declared subscriptions/publications
- `history()` returns last N from retention, empty list if retention=0
- `wait_for()` resolves on matching message
- `wait_for()` raises `RequestTimeoutError` on timeout
- `echo()` yields messages in real-time, stops after `n`
- Socket server: connect, send `{"cmd": "topics"}`, receive valid JSON response (integration test)

---

## Phase 4 — Harness layer

The LLM interaction loop. **Zero imports from `agentbus.bus`, `agentbus.topic`, or `agentbus.node`.** The harness is a standalone library connected to the bus only via the `tool_executor` callback passed to `Harness(...)`.

### Files
- `agentbus/harness/__init__.py` — `Harness` public API
- `agentbus/harness/loop.py` — agent loop, `HarnessDeps`, `production_deps()`
- `agentbus/harness/session.py` — `Session`, `ConversationTurn`, branching, persistence
- `agentbus/harness/compaction.py` — `MicroCompact`, `AutoCompact`, constants
- `agentbus/harness/extensions.py` — `Extension` protocol, pipeline runner
- `agentbus/harness/providers/__init__.py` — `Provider` protocol, `Chunk`, `ToolSchema`, `SystemPrompt`
- `agentbus/harness/providers/ollama.py` — `OllamaProvider`
- `agentbus/harness/providers/anthropic.py` — `AnthropicProvider`
- `agentbus/harness/providers/openai.py` — `OpenAIProvider`
- `tests/test_harness_loop.py`
- `tests/test_harness_session.py`
- `tests/test_harness_compaction.py`
- `tests/test_harness_extensions.py`

### Spec

**`HarnessDeps` protocol (PRD §11.4):**
```python
class HarnessDeps(Protocol):
    async def call_provider(self, messages, tools, **kwargs) -> AsyncIterator[Chunk]: ...
    async def microcompact(self, messages) -> list[ConversationTurn]: ...
    async def autocompact(self, messages) -> CompactResult: ...
    def uuid(self) -> str: ...
```
`production_deps(provider, ...)` wires real implementations. Tests inject fakes with canned responses.

**`Provider` protocol:**
```python
class Provider(Protocol):
    async def complete(self, messages, tools, temperature, max_tokens, stop, signal) -> AsyncIterator[Chunk]: ...
    @property
    def context_window(self) -> int: ...
    def count_tokens(self, messages) -> int: ...
```

**`SystemPrompt` (PRD §11.5 — prompt cache boundary):**
```python
@dataclass
class SystemPrompt:
    static_prefix: str      # tool schemas, base instructions — cacheable across calls
    dynamic_suffix: str     # session context, user prefs, timestamp — per-call

    def render(self) -> list[dict]:
        return [
            {"type": "text", "text": self.static_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": self.dynamic_suffix},
        ]

    def render_plain(self) -> str:
        return self.static_prefix + "\n" + self.dynamic_suffix
```
API providers (Anthropic) use `render()` for prompt caching. Local providers (Ollama) use `render_plain()`. The harness calls whichever method the provider expects.

**`Extension` protocol:** all hooks have default passthrough implementations (not abstract — implement any subset). Pipeline: each hook's output feeds the next extension's input, in registration order.
- `on_context(messages) -> messages`
- `on_before_llm(messages, tools) -> (messages, tools)`
- `on_tool_call(tool_call) -> tool_call | None` (None blocks)
- `on_tool_result(tool_call, result) -> result`
- `on_before_compact(messages) -> messages | None` (None uses default)
- `on_response(response) -> response`
- `on_error(error) -> str | None` (str is fallback response; None re-raises)

**`Session`:**
- Persists to `~/.agentbus/sessions/{session_id}/main.json`
- `fork(from_turn_index: int) -> Session` — creates `branch_{n}.json`, original untouched
- Each `ConversationTurn` tracks `token_count`
- Load/save are plain JSON file I/O — no database

**Compaction (three-layer, PRD §11.2):**
- `MicroCompact`: runs after every tool result, no LLM call. Truncates stale tool outputs to placeholder: `[tool output truncated: {N} tokens → {M} tokens]`
- `AutoCompact`: triggered when token count exceeds `context_window - AUTOCOMPACT_BUFFER_TOKENS`. Calls LLM with compaction prompt. Circuit breaker: `MAX_CONSECUTIVE_COMPACT_FAILURES = 3`.
- `FullCompact`: user-triggered or on session resume. Compresses entire conversation, re-injects recent files (capped at `FILE_REINJECT_CAP_TOKENS` per file). Resets budget to `POST_COMPACT_BUDGET_TOKENS`. **MVP implementation: same as AutoCompact but over the full history. The selective re-injection is a stretch goal.**
- Constants: `AUTOCOMPACT_BUFFER_TOKENS = 13_000`, `MAX_SUMMARY_TOKENS = 20_000`, `FILE_REINJECT_CAP_TOKENS = 5_000`, `POST_COMPACT_BUDGET_TOKENS = 50_000`

**Agent loop (`harness.run(user_input)` — PRD §7.7):**
1. Append user turn to session
2. Run `on_context` extensions
3. Check token count → compact if over threshold
4. Loop (max `max_iterations=25`):
   - Run `on_before_llm` extensions
   - Call `deps.call_provider(messages, tools)`
   - Accumulate streamed chunks into response
   - If response contains tool calls:
     - For each tool call: run `on_tool_call` → if not blocked: call `tool_executor(tool_call)` → run `on_tool_result` → append to session
     - Continue loop
   - If text only (no tool calls):
     - Run `on_response` → append to session → persist → return
5. If `max_iterations` exceeded: call provider with empty tools list to force text response
6. On error: run `on_error` extensions; return fallback string or re-raise

**Streaming fallback (PRD §11.3):**
- `ProviderCall.source: Literal["foreground", "background"]`
- Foreground: on streaming failure → retry non-streaming; after 3 consecutive 529s → demote to next provider
- Background (compaction): fail fast, no fallback, surface error

**Providers:**
- `OllamaProvider`: `httpx` async; streams via `/api/chat`; `context_window` from `/api/show`
- `AnthropicProvider`: `anthropic` SDK; uses `SystemPrompt.render()` for prompt caching; streaming
- `OpenAIProvider`: `openai` SDK; `base_url` for compatible endpoints (vLLM, LM Studio, etc.)
- `MLXProvider`: **not implemented in MVP** — stub raises `NotImplementedError`

### Tests
- **Loop:** inject fake `HarnessDeps` returning one tool call then text → verify `tool_executor` called, final text returned
- **Loop max_iterations:** set `max_iterations=2`, inject infinite tool calls → verify forced text response on iteration 3
- **Compaction circuit breaker:** 3 consecutive `AutoCompact` failures → no more compaction attempts
- **MicroCompact:** 5000-token tool output → truncated to placeholder
- **Session:** create → persist → load → equal. Fork at turn 3 → branch file exists, main untouched.
- **Extensions pipeline:** 3 extensions on `on_context`, verify chain order (each sees previous output)
- **Extension blocking:** `on_tool_call` returns None → `tool_executor` not called
- **SystemPrompt:** `render()` returns list with cache_control on first block; `render_plain()` returns concatenated string

---

## Phase 5 — CLI and Launch

### Files
- `agentbus/cli.py` — CLI commands
- `agentbus/launch.py` — YAML launcher
- `tests/test_cli.py`
- `tests/test_launch.py`

### Spec

**CLI** (uses `typer`, optional dependency under `cli` extra):
```
agentbus topic list
agentbus topic echo <topic> [--n N]
agentbus node list
agentbus node info <name>
agentbus graph [--format mermaid|dot|json]
agentbus launch <config.yaml>
```

CLI connects to a running bus via Unix domain socket. All introspection commands are read-only. `topic echo` streams newline-delimited JSON until interrupted or `--n` reached.

**Launch (`agentbus launch <config.yaml>`):**
- Parse YAML (PRD §6 format)
- Dynamically import node classes via dotted path (`importlib.import_module` + `getattr`)
- Instantiate topics with declared schemas (also imported by dotted path)
- Register topics, then nodes
- Call `bus.spin()`
- Config supports: `bus.heartbeat_interval`, `bus.introspection_socket`, `bus.global_retention`, per-topic `retention`/`schema`/`description`, per-node `class`/`concurrency`/`config`

### Tests
- `agentbus graph --format json` outputs valid JSON with correct nodes/topics/edges (mock socket)
- Launch: parse a minimal YAML config, verify topics and nodes created correctly (unit test, not subprocess)

---

## Phase 6 — Integration and examples

### Files
- `agentbus/nodes/__init__.py`
- `agentbus/nodes/observer.py` — `ObserverNode`
- `agentbus/gateway.py` — `GatewayNode` base (v2 prep)
- `agentbus/__init__.py` — public API exports
- `examples/echo_agent/main.py` — self-contained runnable example
- `tests/test_integration.py`

### Spec

**`ObserverNode`:**
- `subscriptions = ["/system/*"]`, `publications = []`
- Logs lifecycle events at INFO, backpressure at WARNING, telemetry at DEBUG
- Read-only — demonstrates wildcard subscription and zero-publish node

**`GatewayNode` base:**
- Abstract `_listen_external()` and `_send_external(msg)`
- `on_init` creates an `asyncio.Task` for `_listen_external`
- `on_message` delegates `/outbound` to `_send_external`
- No concrete implementations in v1

**Public API (`agentbus/__init__.py`):**
```python
from agentbus.bus import MessageBus
from agentbus.topic import Topic
from agentbus.node import Node, BusHandle
from agentbus.message import Message
from agentbus.gateway import GatewayNode
from agentbus.nodes.observer import ObserverNode
```
Harness imports are separate: `from agentbus.harness import Harness, Extension, Session`

**Example — `examples/echo_agent/main.py`:**
Self-contained, runnable with no LLM. Registers `/inbound` and `/outbound` topics. An `EchoNode` subscribes to `/inbound`, publishes payload text reversed to `/outbound`. An `ObserverNode` watches `/system/*`. Runs `spin(max_messages=3)`. Demonstrates: topic registration, node lifecycle, message routing, spin termination.

### Tests
- End-to-end: echo agent starts, inject 3 messages to `/inbound`, verify 3 messages appear on `/outbound` with correct payloads
- `ObserverNode` receives lifecycle events (at least `NodeStarted` for each node)
- `GatewayNode` subclass: can override `_listen_external` and `_send_external`

---

## Dependency summary

| Package | Required | Used for |
|---|---|---|
| `pydantic>=2.0` | **yes** | Message envelopes, all schemas |
| `httpx` | extra: `ollama` | OllamaProvider async HTTP |
| `anthropic` | extra: `anthropic` | AnthropicProvider |
| `openai` | extra: `openai` | OpenAIProvider |
| `typer` | extra: `cli` | CLI commands |
| `pyyaml` | extra: `cli` | Launch config parsing |
| `pytest` | extra: `dev` | Test runner |
| `pytest-asyncio` | extra: `dev` | Async test support |

---

## What is explicitly NOT in this plan (per PRD §10)

- MLX provider (stub only — `NotImplementedError`)
- Slack/Discord/HTTP gateway implementations
- GUI / TUI dashboard
- Multi-machine / network transport
- Token-by-token streaming out of the bus (internal to nodes)
- Built-in tool nodes (browser, code) — not even stubs

---

## Implementation order rationale

```
Phase 1 (data model) → Phase 2 (topic/node) → Phase 3a (bus/spin core)
                                                       ↓
                                                Phase 3b (introspection + socket)
                                                       ↓
Phase 4 (harness) ←——————————————————————— Phase 3a complete (3b not required)
      ↓
Phase 5 (CLI/launch) ← Phase 3b + Phase 4
      ↓
Phase 6 (integration) ← all above
```

Phase 3a is the critical path. Phase 4 can begin as soon as 3a is done (doesn't need the socket server). Phase 5 needs both 3b (socket) and 4 (harness exists for launch config).
