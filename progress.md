# AgentBus Build Progress

## Status legend
- `[ ]` not started
- `[~]` in progress
- `[x]` complete

---

## Phase 1 — Foundation (Core data model)
- [x] `pyproject.toml` — package metadata, extras (`ollama`, `anthropic`, `openai`, `cli`, `dev`), pytest config
- [x] `agentbus/__init__.py` — empty placeholder
- [x] `agentbus/message.py` — `Message[T]` frozen envelope (nodes never construct directly)
- [x] `agentbus/errors.py` — `TopicSchemaError`, `UndeclaredPublicationError`, `UndeclaredSubscriptionError`, `DuplicateNodeError`, `DuplicateTopicError`, `RequestTimeoutError`, `NodeInitError`, `CircuitBreakerOpenError`
- [x] `agentbus/utils.py` — `CircuitBreaker`
- [x] `agentbus/schemas/__init__.py`
- [x] `agentbus/schemas/system.py` — `LifecycleEvent`, `Heartbeat`, `BackpressureEvent`, `TelemetryEvent`
- [x] `agentbus/schemas/common.py` — `InboundChat`, `OutboundChat`, `ToolRequest`, `ToolResult`
- [x] `agentbus/schemas/harness.py` — `ToolCall`, `PlannerStatus`, `ConversationTurn`
- [x] `tests/conftest.py`
- [x] `tests/test_message.py` — auto-generated fields, frozen, source_node required
- [x] `tests/test_schemas.py` — all schema round-trips
- [x] `tests/test_utils.py` — CircuitBreaker behavior

## Phase 2 — Topic and Node primitives
- [x] `agentbus/topic.py` — `Topic[T]`, retention buffer, fan-out, backpressure, wildcard `matches()`, `validate_payload()`
- [x] `agentbus/node.py` — `Node` ABC, `NodeHandle`, `BusHandle` protocol, `NodeState`, `concurrency_mode`
- [x] `tests/test_topic.py` — retention, fan-out, wildcard, backpressure, schema validation
- [x] `tests/test_node.py` — subclass declaration, no-op defaults, semaphore sizing for serial mode

## Phase 3a — MessageBus core and spin()
- [x] `agentbus/bus.py` — `MessageBus` registration, publish, routing
- [x] `agentbus/introspection.py` — `TopicInfo`, `NodeInfo`, `BusGraph`, `SpinResult`, `Edge` data classes
- [x] `spin()` — VALIDATION phase (orphan/dead-end warnings)
- [x] `spin()` — INIT phase (parallel `on_init`, timeout, error handling, lifecycle events)
- [x] `spin()` — SPIN phase (`_node_loop`, semaphore, error circuit breaker, metrics)
- [x] `spin()` — SHUTDOWN phase (drain, `on_shutdown`, lifecycle events)
- [x] `spin_once()` — process one message, return it
- [x] `spin(until=..., max_messages=..., timeout=...)` — termination variants
- [x] Heartbeat timer task — publishes `Heartbeat` every N seconds
- [x] `/system/*` auto-registration — lifecycle, heartbeat, backpressure, telemetry
- [x] Request/reply — `BusHandle.request()` with correlation_id and timeout
- [x] `tests/test_bus.py` — registration errors, publish routing, spin variants, fan-out, backpressure events, request/reply, circuit breaker, concurrency_mode enforcement

## Phase 3b — Introspection APIs and socket server
- [x] `MessageBus.topics()` — assemble `TopicInfo` from topic internals
- [x] `MessageBus.nodes()` — assemble `NodeInfo` from `NodeHandle` state
- [x] `MessageBus.echo()` — async iterator, temporary subscriber tap
- [x] `MessageBus.graph()` — build `BusGraph` from registrations
- [x] `MessageBus.history()` — last N from retention buffer
- [x] `MessageBus.wait_for()` — block until predicate matches
- [x] Unix domain socket server — start/stop lifecycle, JSON protocol, command dispatch
- [x] `tests/test_introspection.py` — all introspection methods, socket connect/response

## Phase 4 — Harness layer

- [x] `agentbus/harness/providers/__init__.py` — `Provider` protocol, `Chunk`, `ToolSchema`, `SystemPrompt`
- [x] `agentbus/harness/providers/ollama.py` — `OllamaProvider` (httpx, streaming, context_window)
- [x] `agentbus/harness/providers/anthropic.py` — `AnthropicProvider` (SDK, prompt cache via SystemPrompt.render)
- [x] `agentbus/harness/providers/openai.py` — `OpenAIProvider` (SDK, base_url for compatible endpoints)
- [x] `agentbus/harness/extensions.py` — `Extension` base class (all hooks default passthrough), pipeline runner functions
- [x] `agentbus/harness/session.py` — `Session`, JSON persistence, `fork()` branching
- [x] `agentbus/harness/compaction.py` — `MicroCompact`, `AutoCompact`, `FullCompact`, constants
- [x] `agentbus/harness/loop.py` — agent loop, `HarnessDeps` protocol, `ProductionDeps`, `production_deps()`, `ChunkAccumulator`
- [x] `agentbus/harness/__init__.py` — `Harness` public API, exports
- [x] `tests/test_harness_loop.py` — fake deps, tool_executor, max_iterations, forced response
- [x] `tests/test_harness_session.py` — create, persist, load, fork
- [x] `tests/test_harness_compaction.py` — MicroCompact truncation, AutoCompact circuit breaker
- [x] `tests/test_harness_extensions.py` — pipeline chaining, tool_call blocking, SystemPrompt render

## Phase 5 — CLI and Launch
- [x] `agentbus/cli.py` — `topic list`, `topic echo`, `node list`, `node info`, `graph`
- [x] `agentbus/launch.py` — YAML parsing, dynamic import, topic/node registration, `bus.spin()`
- [x] `tests/test_cli.py` — graph JSON output via mock socket
- [x] `tests/test_launch.py` — parse YAML config, verify registration

## Phase 6 — Integration and examples
- [x] `agentbus/nodes/__init__.py`
- [x] `agentbus/nodes/observer.py` — `ObserverNode` (subscribes `/system/*`, logs events)
- [x] `agentbus/gateway.py` — `GatewayNode` base (abstract `_listen_external`, `_send_external`)
- [x] `agentbus/__init__.py` — public API exports (`MessageBus`, `Topic`, `Node`, `Message`, `BusHandle`, `GatewayNode`, `ObserverNode`)
- [x] `examples/echo_agent/main.py` — EchoNode + ObserverNode, `spin(max_messages=3)`, no LLM
- [x] `tests/test_integration.py` — echo agent e2e, ObserverNode receives lifecycle, GatewayNode subclass

---

## Notes

### Phase 3b → Phase 4 handoff

**What was already present before Phase 3b started:**

`topics()`, `nodes()`, and `graph()` were implemented inside `bus.py` at the end of Phase 3a — they weren't net-new in Phase 3b. Phase 3b added `history()`, `wait_for()`, `echo()`, and the socket server.

**Deviations from the plan spec that are intentional and must be preserved:**

1. **`history()` is synchronous** — the plan spec doesn't specify, but since the underlying `topic.history()` is sync and no I/O is involved, `history()` is a plain `def`, not `async def`. The socket server calls it without `await`.

2. **`echo()` uses a 0.5s internal timeout loop** — the generator calls `asyncio.wait_for(queue.get(), timeout=0.5)` and retries on `TimeoutError`. This creates a clean cancellation point at each iteration so that `task.cancel()` on an active echo generator propagates within half a second and the `finally` block removes the temp subscriber.

3. **`wait_for()` raises `RequestTimeoutError` for unregistered topics** — plan spec only specifies the timeout case; the implementation raises immediately with "topic not registered" as the message. Consistent with the error type.

4. **`MessageBus(socket_path=None)` disables the socket server** — pass `None` to skip socket setup entirely. All Phase 3a/3b tests except those explicitly testing the socket use `make_bus()` which defaults to `socket_path=None` (or they rely on the default path, which is fine for most test runs). Tests that do test the socket use the `short_tmp` fixture.

5. **macOS AF_UNIX path limit (104 chars)** — pytest's `tmp_path` creates long paths under `/private/var/folders/...`. Unix socket paths must be ≤104 chars on macOS. The `short_tmp` fixture in `test_introspection.py` uses `tempfile.mkdtemp(dir="/tmp", prefix="ab_")`. Any future test that creates a Unix socket must use this fixture or an equivalent.

**Design pointers for Phase 4 implementation:**

**Build order within Phase 4** — recommended sequence to minimize forward dependencies:
1. `harness/providers/__init__.py` — `Provider` protocol, `Chunk`, `ToolSchema`, `SystemPrompt`
2. `harness/session.py` — `Session`, `fork()`, JSON persistence (`~/.agentbus/sessions/{id}/main.json`)
3. `harness/compaction.py` — `MicroCompact`, `AutoCompact`, constants
4. `harness/extensions.py` — `Extension` protocol, pipeline runner
5. `harness/loop.py` — `HarnessDeps`, `production_deps()`, agent loop
6. `harness/__init__.py` — `Harness` public API
7. Provider implementations (`ollama.py`, `anthropic.py`, `openai.py`) — can be deferred; fake deps cover the loop tests

**`schemas/harness.py` already exists** — `ToolCall`, `PlannerStatus`, and `ConversationTurn` are already defined there. The harness imports them from `agentbus.schemas.harness`. Do not redefine. The `ToolResult` in `schemas/harness.py` is the harness-internal one; alias it when importing alongside `schemas/common.ToolResult`.

**`HarnessDeps` fake for tests** — the test fake should be a class (not a lambda) that takes a list of "turns" to return in sequence: first call returns a tool call chunk, second call returns a text chunk, etc. This lets tests drive the full tool→text flow without touching a real LLM.

**`MicroCompact` is stateless and cheap** — it takes a `list[ConversationTurn]` and returns a new list. No LLM call. Run it after every tool result is appended to the session. Threshold: truncate `tool_result` content if it exceeds a token estimate (rough: `len(content) // 4 > MAX_TOOL_OUTPUT_TOKENS`).

**`AutoCompact` circuit breaker** — use `CircuitBreaker("autocompact", max_failures=3)`. On `is_open`, log a warning and skip compaction rather than raising. The loop continues with whatever context remains.

**Agent loop forced-response path** — when `max_iterations` is exceeded, call the provider again with `tools=[]` (empty tool list). This forces a text-only response. Tests must verify that `tool_executor` is NOT called on this final forced call.

**Streaming accumulation** — `Provider.complete()` yields `Chunk` objects. A `Chunk` carries either a text delta or a tool-call delta (name/arguments fragment). The loop accumulates these into a single `ConversationTurn`. Implement a simple `ChunkAccumulator` dataclass inside `loop.py` — not a separate module.

**Harness zero-import invariant** — add an import check at the top of every `harness/` file: there should be no `from agentbus.bus`, `from agentbus.topic`, or `from agentbus.node` import anywhere under `harness/`. The only permitted agentbus imports are from `agentbus.schemas.*` and `agentbus.errors`.

### Phase 2 → Phase 3a handoff

**Deviations from the plan spec that are intentional and must be preserved:**

1. **`NodeState` uses `CREATED`/`STOPPED`**, not the plan's `INIT`/`SHUTDOWN`. These name the state the node is *in*, not the transition it's *doing*. Phase 3a may add transitional states (`INITIALIZING`, `SHUTTING_DOWN`) if the bus needs to distinguish "currently running `on_init()`" from "hasn't started yet". If not needed, keep as-is.

2. **`Node.on_message(self, msg: Message)`** — takes only `msg`, not `(topic, msg)` as the plan spec pseudocode shows. The topic is available as `msg.topic`. The bus's `_node_loop` must call `await node.on_message(msg)`, not `await node.on_message(msg.topic, msg)`.

3. **`BusHandle` Protocol was extended in Phase 3a** — now exposes `publish(topic, payload)`, `request(topic, payload, reply_on, *, timeout)`, and `topic_history(topic, n)`. The plan spec's pseudocode showed a different `request` signature; the `reply_on` parameter was added as specified.

4. **Backpressure policy is per-topic**, not per-subscriber. `topic.backpressure_policy` is a single `Literal["drop-oldest", "drop-newest"]`. The plan's `"block"` policy is not implemented.

5. **No `TopicMetrics` in `topic.py`** — the bus should track `_total_messages` and per-node counters in `NodeHandle`. `TopicInfo`/`NodeInfo` are assembled by the bus reading topic/handle internals.

6. **`conftest.py` fixtures are still unused** — `inbound_payload`, `tool_request_payload`, `tool_call` are defined but no test file uses them. Phase 4 tests may find them useful for harness layer tests, or they can be removed.

**Design pointers for Phase 3a implementation:**

**`_BusHandle` concrete class** — lives in `bus.py`, satisfies the `BusHandle` Protocol structurally. Holds a back-reference to the bus and the node's name (for `source_node` stamping and publication checks):
```python
class _BusHandle:
    def __init__(self, bus: MessageBus, node_name: str): ...
    async def publish(self, topic: str, payload) -> None:
        # check topic is in node's declared publications
        # delegate to bus.publish(topic, payload, source_node=self._node_name)
    async def request(self, topic: str, payload, *, timeout=30.0) -> Message:
        # correlation_id flow
```

**`_node_loop(node_handle)`** — the per-node async task during SPIN phase. Reads from the single `node_handle.queue`, acquires `node_handle.semaphore`, calls `await node.on_message(msg)`. Error handling via a per-handle `CircuitBreaker("node:{name}", max_failures=10)`. On breaker open → set `handle.state = NodeState.ERROR`, break loop, publish `LifecycleEvent(event="error")`.

**`publish()` flow** (bus-internal, not the BusHandle method):
1. Look up topic in `_topics` dict
2. `topic.validate_payload(payload)` → `TopicSchemaError`
3. Construct `Message(source_node=source_node, topic=topic_name, payload=payload)`
4. `events = topic.put(msg)` — retention + fan-out happen inside `put()`
5. For each event in `events`: recurse `self.publish("/system/backpressure", event, source_node="_bus_")` — guard against infinite recursion (backpressure on the backpressure topic should be dropped or logged, not re-published)
6. Append to `_message_log`, increment `_total_messages`
7. Return the `Message`

**Backpressure recursion guard** — when publishing a `BackpressureEvent` to `/system/backpressure` itself causes backpressure (the subscriber queue for that topic is also full), do NOT recurse. Log the dropped event and move on. A simple guard: skip backpressure re-publish when `source_node == "_bus_"` and `topic_name == "/system/backpressure"`, or use a `_publishing_backpressure: bool` flag.

**`spin_once()` implementation** — process exactly one pending message across all RUNNING nodes. Walk `_nodes`, check each `node_handle.queue` with `get_nowait()`, process the first one found, return it. Use a short `asyncio.wait` with `FIRST_COMPLETED` across all queues if you want to block. This is the primary testing primitive — tests should be able to inject a message into a topic's subscriber queues, call `spin_once()`, and assert on side effects.

**System topic auto-registration** — in `MessageBus.__init__`, create and register:
```python
self.register_topic(Topic[LifecycleEvent]("/system/lifecycle", retention=100))
self.register_topic(Topic[Heartbeat]("/system/heartbeat", retention=1))
self.register_topic(Topic[BackpressureEvent]("/system/backpressure"))
self.register_topic(Topic[TelemetryEvent]("/system/telemetry", retention=50))
```
The bus is the implicit publisher of all `/system/*` topics — it publishes directly without going through `_BusHandle` publication checks.

**`register_node` wiring** — when registering a node:
1. Create `NodeHandle(node)`
2. For each subscription pattern in `node.subscriptions`, find all matching topics via `topic.matches(pattern)`. Raise `UndeclaredSubscriptionError` if zero matches.
3. For each matched topic: `topic.add_subscriber(node.name, node_handle.queue)` — same queue object shared across all subscribed topics.
4. For each publication in `node.publications`, verify at least one topic matches. Raise `UndeclaredPublicationError` if zero matches.
5. Store in `_nodes[node.name]`.

**NodeHandle additions for Phase 3a** — the existing `NodeHandle` class will need per-node metrics fields. Add these in `bus.py` by subclassing or by adding attributes after construction, rather than changing `node.py`. Keep `node.py` as-is — it's the Phase 2 primitive. Alternatively, add optional fields directly to `NodeHandle` with defaults:
- `messages_received: int = 0`
- `messages_published: int = 0`
- `errors: int = 0`
- `error_breaker: CircuitBreaker` (constructed from `MAX_CONSECUTIVE_NODE_ERRORS`)

The simplest path: add these to `NodeHandle.__init__` in `node.py` with defaults so existing tests still pass.
