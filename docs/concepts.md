# Concepts

This document explains the design philosophy behind AgentBus, how its layers
relate to each other, and the invariants that hold across the whole system.

---

## The problem AgentBus solves

Most LLM agent frameworks are monolithic loops. The orchestrator calls a tool,
gets a result, feeds it back to the model, and repeats. This works for a single
agent but becomes brittle when you need:

- **Multiple agents** that hand off work between each other
- **Observability** without modifying agent code
- **Independent deployability** of tools vs. reasoning
- **Schema enforcement** across agent boundaries

AgentBus takes the ROS approach: components communicate through named,
typed topics. The broker (bus) owns routing. Components (nodes) own behavior.
Neither knows the other's internals.

---

## Design principles

**Bus not loop.** The bus is the unit of deployment. Multiple agents can join
the same bus; each is a node. The bus decides when each node runs — not the
other way around.

**Typed topics, not function calls.** Nodes declare what they publish and what
they subscribe to as class-level attributes. Publishing to an undeclared topic
raises an error at runtime. This forces explicit contracts and makes the wiring
inspectable.

**Introspection-first.** The bus exposes its full state — topics, nodes, queue
depths, message history, graph — through a Unix socket that any process can
query while the bus is running. Observability is not opt-in; it's structural.

**Declare, don't discover.** A node that subscribes to `/tools/result` must
declare that in `subscriptions`. A node that publishes to `/tools/request` must
declare that in `publications`. The bus validates topology before spinning and
warns (without raising) about common misconfigurations.

**Harness inside, bus outside.** The LLM loop (`Harness`) has zero imports from
the bus layer. The only connection is a `tool_executor` callback injected at
construction time. This means the harness can be tested without a bus, and
replaced without touching any node.

---

## Layer architecture

```
┌──────────────────────────────────────────────────────────┐
│                      your application                     │
│                                                           │
│   ┌─────────────────────────────────────────────────┐    │
│   │                   MessageBus                     │    │
│   │                                                  │    │
│   │  Topics: /inbound  /tools/request  /outbound    │    │
│   │                                                  │    │
│   │  ┌─────────────┐        ┌──────────────────┐   │    │
│   │  │ PlannerNode │        │  ToolExecutorNode │   │    │
│   │  │             │        │                  │   │    │
│   │  │  ┌────────┐ │        │  pure Python;    │   │    │
│   │  │  │Harness │ │        │  no LLM import   │   │    │
│   │  │  │        │ │        └──────────────────┘   │    │
│   │  │  │Session │ │                                │    │
│   │  │  └────────┘ │                                │    │
│   │  └─────────────┘                                │    │
│   └─────────────────────────────────────────────────┘    │
│                           │                               │
│                    Unix socket                            │
│                           │                               │
│   ┌───────────────────────▼───────────────────────┐      │
│   │          agentbus CLI  /  your dashboard       │      │
│   └───────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────────┘
```

There are three distinct layers:

| Layer | What it owns | What it does NOT touch |
|-------|-------------|----------------------|
| **Bus** | Routing, schema enforcement, fan-out, lifecycle events, introspection | LLM calls, session state, provider specifics |
| **Harness** | LLM loop, session history, context compaction, tool dispatch | Topics, nodes, bus internals |
| **Providers** | Wire-format translation for each LLM API | Agent logic, tool execution |

---

## Core abstractions

### Message[T]

The immutable envelope that wraps every payload on the bus. Nodes never
construct `Message` objects — the bus builds them at publish time, guaranteeing
that `source_node`, `timestamp`, and `id` are set by the infrastructure, not
by the code that published the message.

```python
class Message(BaseModel, Generic[T]):
    id: str             # UUID, auto-generated
    timestamp: datetime # UTC, set at publish time
    source_node: str    # set by bus — nodes cannot forge this
    topic: str          # the topic name the message was published to
    correlation_id: str | None   # used by request/reply pattern
    payload: T          # the typed payload
```

The `frozen=True` model config means a `Message` is immutable once created.
`source_node` integrity is a hard invariant: a node cannot claim to be another
node by constructing a `Message` with a different `source_node`.

### Topic[T]

A named channel with a declared payload type. Topics are parameterized at
class creation time:

```python
topic = Topic[ToolRequest]("/tools/request", retention=10)
topic.schema  # → ToolRequest
```

The bus validates every payload against `topic.schema` before fan-out. A
`TopicSchemaError` is raised if the type does not match.

Topics own three orthogonal features:
- **Fan-out**: deliver a message to every subscribed node's queue
- **Retention**: keep the last N messages in a buffer for history queries
- **Backpressure**: when a subscriber's queue is full, drop messages according
  to `backpressure_policy` (`"drop-oldest"` or `"drop-newest"`)

### Node

An agent component. Subclass `Node`, declare class attributes, override the
lifecycle hooks you need:

```python
class PlannerNode(Node):
    name = "planner"
    subscriptions = ["/inbound"]
    publications = ["/outbound", "/tools/request"]
    concurrency_mode = "serial"

    async def on_init(self, bus: BusHandle) -> None: ...
    async def on_message(self, msg: Message) -> None: ...
    async def on_shutdown(self) -> None: ...
```

Nodes receive a `BusHandle` in `on_init` — a narrow interface that lets them
publish, make request/reply calls, and query topic history. They never hold a
reference to the `MessageBus` itself.

### MessageBus

The broker. It registers topics and nodes, validates topology, runs the spin
lifecycle, and exposes an introspection API:

```python
bus = MessageBus(socket_path="/tmp/myapp.sock")
bus.register_topic(Topic[InboundChat]("/inbound", retention=100))
bus.register_node(PlannerNode())
await bus.spin()
```

### Harness

The self-contained LLM loop. It is **not** a Node. It lives inside a node as
a private component, receiving messages via `on_message` and dispatching tool
calls through the bus via a `tool_executor` callback:

```python
class PlannerNode(Node):
    async def on_init(self, bus):
        self._harness = Harness(
            provider=AnthropicProvider(model="claude-haiku-4-5-20251001"),
            tool_executor=self._execute_tool,
            tools=[...],
            session=Session(),
        )

    async def on_message(self, msg):
        response = await self._harness.run(msg.payload.text)
        await self._bus.publish("/outbound", OutboundChat(text=response))
```

---

## Message lifecycle

```
bus.publish(topic, payload)
     │
     ├─ validate payload type against topic.schema
     ├─ build Message(id, timestamp, source_node, topic, payload)
     ├─ append to topic retention buffer
     ├─ fan-out to each subscriber queue
     │    ├─ if queue full → apply backpressure_policy
     │    └─ emit BackpressureEvent to /system/backpressure
     └─ check _pending_requests for correlation_id matches
          └─ resolve request/reply futures
```

Inside `spin()`, each node has an independent `asyncio.Task` (`_node_loop`) that:

1. Reads from its queue
2. Acquires the node's semaphore (respects `concurrency_mode`)
3. Calls `on_message(msg)`
4. Handles exceptions, updates circuit breaker
5. Increments `processed` counter; checks termination conditions

---

## The request/reply pattern

`BusHandle.request()` provides RPC-over-bus semantics:

```python
# In PlannerNode:
reply = await self._bus.request(
    "/tools/request",
    ToolRequest(tool="calculate", params={"expression": "2**32"}),
    reply_on="/tools/result",
    timeout=30.0,
)
```

Under the hood:
1. A UUID `correlation_id` is generated and stored in `_pending_requests`
2. The message is published with that `correlation_id`
3. The caller `await`s a `Future`
4. When any message arrives on `/tools/result` with the matching `correlation_id`,
   the bus resolves the future
5. On timeout, `RequestTimeoutError` is raised and the future is cancelled

The responding node must echo the correlation_id back:

```python
# In ToolExecutorNode:
await self._bus.publish(
    "/tools/result",
    ToolResult(...),
    correlation_id=msg.correlation_id,
)
```

---

## Wildcard subscriptions

Topic patterns in `subscriptions` support two wildcards:

| Pattern | Meaning |
|---------|---------|
| `*` | Exactly one path segment |
| `**` | Zero or more path segments |

Examples:
```python
subscriptions = ["/system/*"]   # matches /system/lifecycle, /system/heartbeat
                                 # does NOT match /system/foo/bar
subscriptions = ["/tools/**"]   # matches /tools/request, /tools/result,
                                 # /tools/calc/result
```

Wildcards can appear at any position: `/*/request`, `/**/events`.

---

## Concurrency model

`concurrency_mode = "parallel"` (default): the node's semaphore allows up to
`concurrency` (default: 1) concurrent `on_message` calls. Parallel nodes can
process multiple messages simultaneously if `concurrency > 1`.

`concurrency_mode = "serial"`: always uses `Semaphore(1)` regardless of
`node.concurrency`. Use this for nodes that mutate state (session, filesystem,
database).

The Harness loop is stateful, so `PlannerNode` should always be `serial`.

---

## Circuit breakers

Every retry loop is guarded by a `CircuitBreaker`. When consecutive failures
exceed the threshold, the breaker opens and further calls are rejected without
attempting the operation.

| Location | Breaker name | Max failures |
|----------|-------------|-------------|
| Per-node error handling | `node:{name}` | 10 |
| AutoCompact | `autocompact` | 3 |

When a node's breaker opens, its state transitions to `NodeState.ERROR` and its
`_node_loop` exits. The node stops receiving messages.

---

## Key invariants

- `source_node` on a `Message` is always set by the bus, never by the node.
- Publishing to a topic not in `publications` raises `UndeclaredPublicationError`.
- Publishing a wrong-type payload raises `TopicSchemaError`.
- `on_message` exceptions never crash the node — they are caught, logged, and
  published to `/system/lifecycle`.
- The harness has zero imports from the bus layer.
- `Session.fork()` never mutates the parent session.
- `spin_once()` is the primary testing primitive — it works without a running
  `spin()` loop.
