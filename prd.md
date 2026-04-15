# AgentBus PRD

> ROS-inspired typed message bus for agentic LLM orchestration.
> Local-first. asyncio-native. Introspection-first.

---

## 1. Problem

Every agent framework today is a monolithic loop: LLM → tool → LLM → tool. Debugging is guesswork. Adding a new tool means touching the orchestrator. Swapping a model means rewriting the harness. There is no standard for agent-internal communication, no observability layer, and no separation between routing and execution.

ROS solved an isomorphic problem for robotics 15 years ago. AgentBus ports the philosophy — typed pub/sub, node lifecycle, introspection-by-default — to LLM agent orchestration.

## 2. Design principles

- **Bus, not loop.** Nodes communicate through typed topics, not function calls. The planner doesn't import the tool — it publishes a message. The tool doesn't return to the planner — it publishes a result.
- **Introspection is not optional.** Every message, every subscription, every node state transition is observable from outside. If you can't `echo` it, it doesn't exist.
- **Gateway-ready, gateway-deferred.** The topic abstraction is the interface boundary for external I/O (Slack, Discord, HTTP — OpenClaw-style). v1 ships without gateways, but the design must make adding one a zero-change operation on existing nodes.
- **Local-first.** In-process asyncio on a single machine. No serialization overhead, no network stack, no containers. The abstraction layer is clean enough to swap in Redis/NATS/ZMQ later, but v1 assumes a MacBook.
- **Declare, don't discover.** Nodes declare their subscriptions and publications at init. The bus knows the full topology before any message flows. This enables static validation, graph rendering, and dead-topic detection.
- **Harness inside, bus outside.** The LLM agent loop (prompt → LLM → tool call → result → repeat) is an internal concern of a single node. The bus routes messages between nodes. The harness manages the tight loop; the bus manages the topology. Neither knows about the other's internals.

## 3. Core abstractions

### 3.1 `Message[T]`

The envelope. Every message on the bus is generic over a Pydantic `BaseModel` payload.

```python
class Message[T](BaseModel):
    id: str                          # uuid4, auto-generated
    timestamp: datetime              # utc, auto-generated
    source_node: str                 # name of publishing node
    topic: str                       # topic it was published to
    correlation_id: str | None       # links request ↔ reply chains
    payload: T                       # the actual typed data
```

Design notes:

- `correlation_id` enables request/reply over pub/sub. A ToolNode publishes a result with the same `correlation_id` as the request it received. The PlannerNode can filter or await on correlation.
- `source_node` is set by the bus at publish time, not by the node. Nodes cannot spoof their identity.
- The envelope is immutable after creation. Nodes receive a frozen copy.

### 3.2 `Topic[T]`

A named, typed channel bound to a single Pydantic model.

```python
topic = Topic[ToolRequest](
    name="/tools/request",
    retention=50,            # keep last 50 messages (default: 0)
    description="Tool execution requests from the planner"
)
```

Capabilities:

- **Schema enforcement.** Publishing a payload that doesn't match `T` raises `TopicSchemaError` at publish time, not at consumption time.
- **Fan-out.** Multiple subscribers receive the same message independently. Each subscriber has its own asyncio queue.
- **Retention.** Optional ring buffer of the last N messages. Enables late-joining nodes and context reconstruction (e.g., PlannerNode reads recent `/tools/result` history to rebuild state after restart).
- **Wildcard subscription.** Subscribing to `/tools/*` receives messages from `/tools/request` and `/tools/result`. Implemented via prefix matching on topic names, not regex.

Topic naming convention:

```
/inbound            # messages arriving from external channels (gateway)
/outbound           # messages leaving to external channels (gateway)
/planning/tasks     # planner's task decomposition output
/planning/status    # planner state transitions (thinking, blocked, done)
/tools/request      # tool execution requests
/tools/result       # tool execution results
/memory/query       # retrieval requests to memory node
/memory/context     # retrieved context chunks
/system/lifecycle   # node start/stop/error events (auto-published by bus)
/system/heartbeat   # periodic liveness signals (auto-published by bus)
```

### 3.3 `Node`

The unit of computation. Abstract base class with a declared lifecycle.

```python
class PlannerNode(Node):
    name = "planner"
    concurrency = 1                         # max parallel on_message calls
    subscriptions = ["/inbound", "/tools/result", "/memory/context"]
    publications = ["/planning/tasks", "/tools/request", "/memory/query", "/outbound"]

    async def on_init(self, bus: BusHandle):
        """Called once before spin. Load model, warm caches."""
        self.model = load_model("mlx-community/...")

    async def on_message(self, topic: str, msg: Message):
        """Called for each message on subscribed topics."""
        if topic == "/inbound":
            plan = await self.reason(msg.payload)
            await self.publish("/tools/request", ToolRequest(...))
        elif topic == "/tools/result":
            ...

    async def on_shutdown(self):
        """Called once during graceful shutdown."""
        self.model.unload()
```

Node contract:

- `name` — unique string identifier. The bus rejects duplicate names at registration.
- `concurrency` — max parallel invocations of `on_message`. Default 1 (sequential). For LLM nodes, this should always be 1. For I/O-bound tool nodes, set higher.
- `subscriptions` / `publications` — declared at class level. The bus validates these against the topic registry at registration time. Publishing to an undeclared topic raises `UndeclaredPublicationError`. This is the "compile-time guarantee" equivalent.
- `on_init` receives a `BusHandle` — a restricted view of the bus that exposes `publish()`, `topic_history()`, and `request()` (for request/reply pattern) but not raw bus internals.
- `on_message` receives the topic name and the full `Message` envelope. The node can pattern-match on topic.
- Exceptions in `on_message` are caught by the bus, logged, published to `/system/lifecycle` as error events, and do not crash the node. The node continues processing subsequent messages.

### 3.4 `MessageBus`

The broker. Owns the event loop, topic registry, node registry, and introspection state.

```python
bus = MessageBus()

# Register topics
bus.register_topic(Topic[InboundChat]("/inbound", retention=10))
bus.register_topic(Topic[ToolRequest]("/tools/request", retention=50))
bus.register_topic(Topic[ToolResult]("/tools/result", retention=50))
bus.register_topic(Topic[MemoryQuery]("/memory/query"))
bus.register_topic(Topic[MemoryContext]("/memory/context", retention=20))
bus.register_topic(Topic[OutboundChat]("/outbound", retention=10))

# Register nodes
bus.register_node(PlannerNode())
bus.register_node(BrowserToolNode())
bus.register_node(CodeToolNode())
bus.register_node(MemoryNode())
bus.register_node(ObserverNode())

# Run
bus.spin()
```

Internal structure:

- `_topics: dict[str, Topic]` — topic registry.
- `_nodes: dict[str, NodeHandle]` — node registry. Each `NodeHandle` wraps the node instance, its subscription queues, concurrency semaphore, and lifecycle state.
- `_subscriptions: dict[str, list[NodeHandle]]` — topic name → list of subscribed node handles. Built from node declarations at registration.
- `_message_log: deque[Message]` — global message log (configurable max size). Feeds the introspection layer.
- `_metrics: dict[str, TopicMetrics]` — per-topic counters: messages published, messages delivered, publish rate (rolling 60s window), last message timestamp.

---

## 4. `spin()` — the event loop

### 4.1 Lifecycle

```
bus.spin() is called
    │
    ├── Phase 1: VALIDATION
    │   ├── Check all node subscriptions reference registered topics
    │   ├── Check all node publications reference registered topics
    │   ├── Detect orphan topics (registered but no subscribers/publishers)
    │   ├── Detect dead-end nodes (subscribes but never publishes, or vice versa)
    │   └── Log warnings for any issues (don't fail — warn)
    │
    ├── Phase 2: INIT
    │   ├── Call on_init() on each node (parallel, with timeout)
    │   ├── Publish NodeStarted events to /system/lifecycle
    │   ├── Start heartbeat timer
    │   └── If any on_init() fails: log, publish NodeInitFailed, skip that node
    │
    ├── Phase 3: SPIN (main loop)
    │   ├── For each node, launch an asyncio task: _node_loop(node)
    │   │   └── while running:
    │   │       ├── await message from any subscribed topic queue
    │   │       ├── acquire concurrency semaphore
    │   │       ├── call node.on_message(topic, msg)
    │   │       ├── release semaphore
    │   │       └── update metrics
    │   ├── Heartbeat task: publish to /system/heartbeat every N seconds
    │   └── Introspection server task (see §5)
    │
    ├── Phase 4: SHUTDOWN (on SIGINT/SIGTERM/bus.stop())
    │   ├── Set running = False
    │   ├── Drain all queues (with timeout — don't hang on a stuck node)
    │   ├── Call on_shutdown() on each node (parallel, with timeout)
    │   ├── Publish NodeStopped events to /system/lifecycle
    │   └── Close introspection server
    │
    └── Return
```

### 4.2 `spin()` implementation contract

```python
def spin(
    self,
    until: Callable[[], bool] | None = None,   # spin until predicate returns True
    max_messages: int | None = None,            # spin for N messages then stop
    timeout: float | None = None,               # spin for N seconds then stop
) -> SpinResult:
```

- Default (no args): spin forever until SIGINT/SIGTERM.
- `until`: callable checked after each message delivery. Enables scripted workflows: "run until the planner publishes to /outbound."
- `max_messages`: for testing and batch jobs. Process exactly N messages across all topics, then shut down.
- `timeout`: wall-clock deadline.
- Returns `SpinResult` with summary stats: messages processed, duration, per-node counts, any errors.

### 4.3 `spin_once()`

Process exactly one pending message across all nodes. Blocks until a message is available or timeout expires.

```python
async def spin_once(self, timeout: float = 5.0) -> Message | None:
```

Primary use: **testing.** Publish a message, call `spin_once()`, assert on the result.

```python
async def test_tool_routing():
    bus = make_test_bus()
    await bus.publish("/tools/request", ToolRequest(tool="browser", url="..."))
    msg = await bus.spin_once(timeout=2.0)
    assert msg.topic == "/tools/result"
    assert msg.payload.status == "success"
```

### 4.4 Message routing inside spin

When a node calls `self.publish(topic, payload)`:

1. Bus wraps payload in `Message[T]` envelope (auto-generates id, timestamp, sets source_node).
2. Bus validates payload against the topic's Pydantic schema.
3. Bus appends to topic's retention buffer (if configured).
4. Bus appends to global message log.
5. Bus pushes message to every subscriber's queue for that topic.
6. Bus updates topic metrics.

Each subscriber queue is an `asyncio.Queue` with configurable max size (default 100). If a subscriber's queue is full (node is slow), the bus applies backpressure:

- **Default: drop-oldest.** Oldest message in the queue is evicted. Logged as a warning.
- **Configurable per-subscription:** `drop-oldest`, `drop-newest` (discard incoming), or `block` (bus.publish awaits until space is available — use with care).

### 4.5 Request/reply pattern

For synchronous-style interactions over the async bus (e.g., "query memory and wait for context"):

```python
# Inside PlannerNode.on_message:
response = await self.request(
    publish_to="/memory/query",
    payload=MemoryQuery(query="FCA architecture details"),
    reply_on="/memory/context",
    timeout=5.0
)
# response is Message[MemoryContext] with matching correlation_id
```

Internally: generates a `correlation_id`, publishes the request, then awaits the first message on the reply topic with that `correlation_id`. Times out with `RequestTimeoutError` if no reply arrives.

---

## 5. Introspection

Introspection is the core differentiator. Every ROS equivalent is present, plus agent-specific additions.

### 5.1 Programmatic API

Available on the `MessageBus` instance and via the `BusHandle` passed to nodes.

#### `bus.topics() → list[TopicInfo]`

List all registered topics with metadata.

```python
@dataclass
class TopicInfo:
    name: str                    # "/tools/request"
    schema: type[BaseModel]      # ToolRequest
    retention: int               # 50
    subscriber_count: int        # 2
    publisher_count: int         # 1
    message_count: int           # 347 (total ever published)
    rate_hz: float               # 2.3 (rolling 60s window)
    last_message_at: datetime    # 2026-04-13T14:23:01Z
    queue_depths: dict[str, int] # {"planner": 0, "observer": 3}
```

#### `bus.nodes() → list[NodeInfo]`

List all registered nodes with state.

```python
@dataclass
class NodeInfo:
    name: str                     # "planner"
    state: NodeState              # RUNNING | INIT | SHUTDOWN | ERROR
    concurrency: int              # 1
    active_tasks: int             # 0 (currently processing)
    subscriptions: list[str]      # ["/inbound", "/tools/result"]
    publications: list[str]       # ["/tools/request", "/outbound"]
    messages_received: int        # 891
    messages_published: int       # 456
    errors: int                   # 2
    mean_latency_ms: float        # 1450.0 (on_message wall time)
    uptime_s: float               # 3600.0
```

#### `bus.echo(topic, n=None, filter=None) → AsyncIterator[Message]`

Stream messages on a topic in real-time.

```python
async for msg in bus.echo("/tools/result", n=5):
    print(f"[{msg.source_node}] {msg.payload}")
# Prints the next 5 messages, then stops.

# With filter:
async for msg in bus.echo("/tools/result", filter=lambda m: m.payload.status == "error"):
    handle_error(msg)
```

#### `bus.graph() → BusGraph`

Full node-topic connectivity graph. Serializable to dict/JSON.

```python
@dataclass
class BusGraph:
    nodes: list[NodeInfo]
    topics: list[TopicInfo]
    edges: list[Edge]            # (node, topic, direction: "sub"|"pub")
```

Enables rendering the topology as a diagram (equivalent to `rqt_graph`).

#### `bus.history(topic, n=10) → list[Message]`

Last N messages from a topic's retention buffer. Returns fewer if the buffer hasn't filled yet.

#### `bus.wait_for(topic, predicate, timeout) → Message`

Block until a message matching predicate appears on topic. Useful for scripting and testing.

```python
result = await bus.wait_for(
    "/outbound",
    predicate=lambda m: m.correlation_id == task_id,
    timeout=30.0
)
```

### 5.2 CLI

Wraps the programmatic API. Connects to a running bus via a unix domain socket (local-only, no network exposure).

```
$ agentbus topic list
NAME                SCHEMA          SUBS  PUBS  RATE     DEPTH
/inbound            InboundChat     1     0     0.1/s    planner:0
/planning/tasks     TaskPlan        0     1     0.1/s    —
/tools/request      ToolRequest     2     1     0.3/s    browser:0 code:1
/tools/result       ToolResult      1     2     0.3/s    planner:0
/memory/query       MemoryQuery     1     1     0.1/s    memory:0
/memory/context     MemoryContext   1     1     0.1/s    planner:0
/outbound           OutboundChat    0     1     0.1/s    —
/system/lifecycle   LifecycleEvent  1     0     —        observer:0
/system/heartbeat   Heartbeat       1     0     0.03/s   observer:0

$ agentbus topic echo /tools/request
[14:23:01.234] planner → /tools/request
  ToolRequest(tool="browser", action="navigate", url="https://arxiv.org/...")

[14:23:03.891] planner → /tools/request
  ToolRequest(tool="code", action="execute", code="import pandas as...")

^C

$ agentbus node list
NAME        STATE    CONCUR  ACTIVE  RECV   PUB    ERR  LATENCY
planner     RUNNING  1       0       891    456    2    1450ms
browser     RUNNING  2       1       234    234    0    3200ms
code        RUNNING  4       0       112    112    0    890ms
memory      RUNNING  2       0       200    200    0    45ms
observer    RUNNING  1       0       1891   0      0    1ms

$ agentbus node info planner
Name:           planner
State:          RUNNING
Concurrency:    1 (0 active)
Uptime:         1h 23m
Subscribes to:  /inbound, /tools/result, /memory/context
Publishes to:   /planning/tasks, /tools/request, /memory/query, /outbound
Messages in:    891  (0.2/s avg)
Messages out:   456  (0.1/s avg)
Errors:         2
Mean latency:   1450ms
P99 latency:    4200ms
Queue depths:   /inbound:0  /tools/result:0  /memory/context:0

$ agentbus graph --format mermaid
graph LR
  planner -->|pub| /tools/request
  planner -->|pub| /memory/query
  planner -->|pub| /outbound
  /inbound -->|sub| planner
  /tools/result -->|sub| planner
  /memory/context -->|sub| planner
  browser -->|pub| /tools/result
  /tools/request -->|sub| browser
  ...

$ agentbus graph --format dot | dot -Tpng -o topology.png
```

### 5.3 Built-in `/system` topics

These are auto-registered by the bus. Nodes don't publish to them — the bus does.

#### `/system/lifecycle`

Published automatically on node state transitions.

```python
class LifecycleEvent(BaseModel):
    node: str
    event: Literal["started", "stopped", "error", "init_failed"]
    error: str | None = None
    timestamp: datetime
```

#### `/system/heartbeat`

Published by the bus on a fixed interval (default: 30s). Contains a snapshot of bus health.

```python
class Heartbeat(BaseModel):
    uptime_s: float
    node_count: int
    topic_count: int
    total_messages: int
    messages_per_second: float       # rolling 60s
    node_states: dict[str, str]      # {"planner": "RUNNING", ...}
    queue_depths: dict[str, int]     # {"planner": 0, "browser": 3}
```

#### `/system/backpressure`

Published when a subscriber queue drops a message due to overflow.

```python
class BackpressureEvent(BaseModel):
    topic: str
    subscriber_node: str
    queue_size: int
    dropped_message_id: str
    policy: Literal["drop-oldest", "drop-newest"]
```

### 5.4 Observer protocol

Any node can subscribe to `/system/*` for full observability. The built-in `ObserverNode` demonstrates the pattern:

```python
class ObserverNode(Node):
    name = "observer"
    subscriptions = ["/system/*"]    # wildcard: all system topics
    publications = []                # read-only node

    async def on_message(self, topic: str, msg: Message):
        if topic == "/system/lifecycle":
            log.info(f"[lifecycle] {msg.payload.node}: {msg.payload.event}")
        elif topic == "/system/backpressure":
            log.warning(f"[backpressure] {msg.payload.subscriber_node} dropping on {msg.payload.topic}")
```

Users can extend or replace ObserverNode with custom logging, alerting, or metrics export (Prometheus, file-based, etc.).

---

## 6. Launch configuration

Declarative topology definition. Equivalent to ROS launch files.

```yaml
# agentbus.yaml
bus:
  heartbeat_interval: 30
  introspection_socket: /tmp/agentbus.sock
  global_retention: 0            # per-topic overrides below

topics:
  /inbound:
    schema: agentbus.schemas.InboundChat
    retention: 10
  /tools/request:
    schema: agentbus.schemas.ToolRequest
    retention: 50
  /tools/result:
    schema: agentbus.schemas.ToolResult
    retention: 50
  /memory/query:
    schema: agentbus.schemas.MemoryQuery
  /memory/context:
    schema: agentbus.schemas.MemoryContext
    retention: 20
  /outbound:
    schema: agentbus.schemas.OutboundChat
    retention: 10

nodes:
  planner:
    class: myagent.nodes.PlannerNode
    concurrency: 1
    config:
      model: mlx-community/Meta-Llama-3.1-8B-Instruct-4bit
      temperature: 0.7
  browser:
    class: agentbus.tools.BrowserToolNode
    concurrency: 2
  code:
    class: agentbus.tools.CodeToolNode
    concurrency: 4
  memory:
    class: myagent.nodes.MemoryNode
    concurrency: 2
    config:
      backend: lancedb
      embedding_model: nomic-embed-text
  observer:
    class: agentbus.Observer
```

Launch:

```bash
$ agentbus launch agentbus.yaml
[14:00:00] Registered 7 topics, 5 nodes
[14:00:00] Topology validation: OK (0 warnings)
[14:00:00] Initializing nodes...
[14:00:02] planner: RUNNING (model loaded in 1.8s)
[14:00:02] browser: RUNNING
[14:00:02] code: RUNNING
[14:00:02] memory: RUNNING (lancedb index: 4,231 vectors)
[14:00:02] observer: RUNNING
[14:00:02] Introspection socket: /tmp/agentbus.sock
[14:00:02] Spinning...
```

---

## 7. Harness layer (Pi-inspired)

The bus routes messages. The harness reasons. These are separate concerns that meet inside a single node.

### 7.1 What the harness is

A harness is the component that manages the tight agent loop: assemble context → call LLM → parse response → execute tool calls → feed results back → repeat until done. This is the loop that Pi, Claude Code, and every coding agent runs internally. In AgentBus, the harness is a library that the PlannerNode (or any LLM-powered node) uses internally — it is not a node itself, and it does not know about the bus.

The design follows Pi's philosophy: minimal core, maximal extensibility. Pi ships four tools (read, write, edit, bash) and a 300-word system prompt, then lets extensions add everything else. AgentBus's harness ships zero built-in tools — tools live on the bus as ToolNodes — and focuses purely on the LLM interaction lifecycle: session management, context assembly, provider abstraction, compaction, and extension hooks.

### 7.2 Layered architecture

Mirrors Pi's package stack, adapted to Python and the bus abstraction:

```
┌─────────────────────────────────────────────┐
│  Your Application                           │
│  (PlannerNode, custom HarnessNodes)         │
├──────────────────────┬──────────────────────┤
│  agentbus.harness    │  agentbus (bus)      │
│  Sessions, context,  │  Topics, nodes,      │
│  extensions,         │  spin, introspect    │
│  compaction          │                      │
├──────────────────────┴──────────────────────┤
│  agentbus.harness.loop                      │
│  Agent loop, tool dispatch, events          │
├─────────────────────────────────────────────┤
│  agentbus.harness.providers                 │
│  Streaming, models, multi-provider LLM      │
│  (MLX, Ollama, Anthropic, OpenAI)           │
└─────────────────────────────────────────────┘
```

**`agentbus.harness.providers`** — LLM provider abstraction. Unified interface for local (MLX, Ollama) and remote (Anthropic, OpenAI) models. Handles streaming, token counting, and model-specific quirks (Gemini turn ordering, OpenAI function call format differences). A provider is a callable: `async def complete(messages, tools, **kwargs) -> Stream[Chunk]`.

**`agentbus.harness.loop`** — The agent loop. Takes a user message, a provider, and a tool executor callback. Runs the LLM, parses tool calls from the response, invokes the executor, feeds results back, repeats until the LLM produces a final text response (no tool calls). The loop emits typed events at each stage (see §7.5).

**`agentbus.harness`** (top-level) — Session management, context window tracking, compaction (summarizing old turns to free context space), extension loading, and system prompt assembly from templates + skills.

### 7.3 The bridge: harness ↔ bus

The harness doesn't know about the bus. The PlannerNode connects them via the tool executor callback:

```python
class PlannerNode(Node):
    name = "planner"
    subscriptions = ["/inbound", "/tools/result"]
    publications = ["/tools/request", "/outbound"]

    async def on_init(self, bus: BusHandle):
        provider = OllamaProvider(model="llama3.1:8b")
        self.harness = Harness(
            provider=provider,
            tool_executor=self._execute_tool,     # bridge function
            system_prompt="You are a helpful assistant.",
            extensions=[TokenCounter(), ContextPruner()],
        )

    async def _execute_tool(self, tool_call: ToolCall) -> ToolResult:
        """Bridge: harness calls this, we route through the bus."""
        response = await self.request(
            publish_to="/tools/request",
            payload=ToolRequest(
                tool=tool_call.name,
                params=tool_call.arguments,
            ),
            reply_on="/tools/result",
            timeout=30.0,
        )
        return ToolResult(
            tool_call_id=tool_call.id,
            output=response.payload.output,
            error=response.payload.error,
        )

    async def on_message(self, topic: str, msg: Message):
        if topic == "/inbound":
            result = await self.harness.run(msg.payload.text)
            await self.publish("/outbound", OutboundChat(text=result.text))
```

This is the same pattern OpenClaw uses to embed Pi — replace the tool executor, keep everything else. The key invariant: **the harness thinks it's calling tools directly via a callback. The PlannerNode secretly routes those calls through the bus.** ToolNodes on the other end of `/tools/request` don't know they're being called by a harness — they just see a message.

### 7.4 Session management

Sessions are the conversational state — the history of user messages, assistant responses, and tool interactions across multiple turns.

```python
class Session:
    id: str                              # unique session identifier
    messages: list[ConversationTurn]      # full history
    metadata: dict[str, Any]             # custom key-value store
    created_at: datetime
    updated_at: datetime

class ConversationTurn(BaseModel):
    role: Literal["user", "assistant", "tool_result"]
    content: str | list[ContentBlock]
    tool_calls: list[ToolCall] | None = None
    token_count: int                     # tracked for compaction
    timestamp: datetime
```

**Persistence.** Sessions serialize to JSON files under a configurable directory (default: `~/.agentbus/sessions/`). Each session is a single file. No database dependency.

**Branching.** Inspired by Pi's session tree model: when the user backtracks ("actually, try a different approach"), the session forks into a new branch from the branch point. Previous branches are preserved, not overwritten. This is essential for exploratory agent workflows where you want to compare different tool chains.

```
session_abc/
├── main.json          # original conversation
├── branch_1.json      # forked at turn 5, tried browser tool
└── branch_2.json      # forked at turn 5, tried code tool
```

**Compaction.** When the conversation exceeds the context window, older turns are summarized. The harness calls the LLM with a compaction prompt: "Summarize the following conversation history, preserving all tool call results, file paths, and error messages." The summary replaces the old turns. Extension hook `before_compact` lets users customize this (e.g., OpenClaw preserves file operation history that the default summarizer would drop).

### 7.5 Extension hooks

Extensions are Python callables registered on lifecycle events. Same philosophy as Pi: the harness emits events, extensions intercept them.

```python
class Harness:
    def __init__(self, ..., extensions: list[Extension] = []):
        ...

class Extension(Protocol):
    """Any subset of these methods can be implemented."""

    async def on_context(
        self, messages: list[ConversationTurn]
    ) -> list[ConversationTurn]:
        """Rewrite context before sending to LLM.
        Use for: injecting RAG results, pruning oversized tool outputs,
        adding time/location context, applying persona overlays."""
        return messages

    async def on_before_llm(
        self, messages: list[ConversationTurn], tools: list[ToolSchema]
    ) -> tuple[list[ConversationTurn], list[ToolSchema]]:
        """Intercept right before the LLM call.
        Use for: filtering available tools based on context,
        injecting one-shot examples, modifying temperature."""
        return messages, tools

    async def on_tool_call(
        self, tool_call: ToolCall
    ) -> ToolCall | None:
        """Intercept a tool call before execution.
        Return None to block. Return modified ToolCall to transform.
        Use for: permission gating, sandboxing, cost limits,
        logging, rate limiting expensive tools."""
        return tool_call

    async def on_tool_result(
        self, tool_call: ToolCall, result: ToolResult
    ) -> ToolResult:
        """Transform a tool result before feeding back to LLM.
        Use for: truncating large outputs, redacting secrets,
        adding metadata the LLM should see."""
        return result

    async def on_before_compact(
        self, messages: list[ConversationTurn]
    ) -> list[ConversationTurn] | None:
        """Customize compaction. Return None to use default summarizer.
        Return modified messages to replace the default compaction.
        Use for: preserving critical tool results that the
        default summarizer would drop."""
        return None

    async def on_response(self, response: str) -> str:
        """Transform the final response before it's returned.
        Use for: formatting, safety filtering, adding citations."""
        return response

    async def on_error(self, error: Exception) -> str | None:
        """Handle errors in the agent loop.
        Return a string to use as the response. Return None to re-raise.
        Use for: graceful degradation, retry logic, fallback models."""
        return None
```

Extensions run in registration order. Each hook's output feeds into the next extension's input (pipeline pattern).

### 7.6 Provider abstraction

```python
class Provider(Protocol):
    """Unified LLM interface."""

    async def complete(
        self,
        messages: list[dict],
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        signal: AbortSignal | None = None,
    ) -> AsyncIterator[Chunk]:
        """Stream completion chunks."""
        ...

    @property
    def context_window(self) -> int:
        """Max tokens for this model."""
        ...

    def count_tokens(self, messages: list[dict]) -> int:
        """Approximate token count for context tracking."""
        ...
```

Built-in providers for v1:

- **`OllamaProvider`** — local models via Ollama API. Preferred path for M4 deployment.
- **`MLXProvider`** — direct MLX inference for Apple Silicon. No server dependency, lowest latency.
- **`AnthropicProvider`** — Claude API for cases where local models aren't sufficient.
- **`OpenAIProvider`** — GPT API, including OpenAI-compatible endpoints (vLLM, LM Studio, etc.).

Provider selection lives in the launch config:

```yaml
nodes:
  planner:
    class: myagent.nodes.PlannerNode
    config:
      provider: ollama                    # or: mlx, anthropic, openai
      model: llama3.1:8b-instruct
      temperature: 0.7
      fallback:                           # provider chain with backoff
        - provider: anthropic
          model: claude-sonnet-4-20250514
```

The fallback chain is inspired by OpenClaw's provider rotation: if the primary provider fails or times out, the harness silently falls through to the next provider. Backoff is exponential per-provider.

### 7.7 The agent loop internals

The harness loop is deliberately simple. No planning layer, no sub-agents, no DAG execution — those are extension or node-level concerns.

```
harness.run(user_input) is called
    │
    ├── Append user_input to session history
    ├── Run on_context extensions (rewrite history)
    ├── Check token count → compact if over threshold
    │
    ├── LOOP:
    │   ├── Run on_before_llm extensions (filter tools, inject context)
    │   ├── Call provider.complete(messages, tools)
    │   ├── Stream response chunks
    │   │
    │   ├── If response contains tool calls:
    │   │   ├── For each tool call:
    │   │   │   ├── Run on_tool_call extensions (gate/transform)
    │   │   │   ├── If not blocked: call tool_executor(tool_call)
    │   │   │   ├── Run on_tool_result extensions (transform result)
    │   │   │   └── Append tool call + result to session
    │   │   └── Continue LOOP (feed results back to LLM)
    │   │
    │   └── If response is text only (no tool calls):
    │       ├── Run on_response extensions (transform)
    │       ├── Append assistant response to session
    │       ├── Persist session to disk
    │       └── Return response
    │
    └── On error at any stage:
        ├── Run on_error extensions
        ├── If extension returns fallback response: return it
        └── Otherwise: raise
```

The loop is bounded by a configurable `max_iterations` (default: 25) to prevent infinite tool call cycles. When exceeded, the harness forces a final response by calling the LLM without any tools available.

### 7.8 Harness-aware introspection

The harness emits events that the bus can observe. The PlannerNode publishes harness lifecycle events to `/planning/status`:

```python
class PlannerStatus(BaseModel):
    event: Literal[
        "thinking",           # LLM call started
        "tool_dispatched",    # tool call sent to bus
        "tool_received",      # tool result received
        "compacting",         # context window compaction triggered
        "responding",         # final response being generated
        "error",              # harness error
    ]
    iteration: int            # which loop iteration (1-based)
    context_tokens: int       # current context window usage
    context_capacity: float   # percentage of context window used
    tool_name: str | None     # for tool_dispatched/tool_received
    detail: str | None        # human-readable description
```

This means `agentbus topic echo /planning/status` shows the harness's internal state transitions in real time — the LLM is thinking, it dispatched a browser call, it got the result, it's thinking again, now it's compacting because it hit 80% context capacity, now it's responding. Full observability without the harness knowing it's being observed.

---

## 8. Gateway interface (v2 — design only)

Not built in v1, but the abstraction must support it without breaking changes.

A gateway is a Node subclass with an external I/O loop:

```python
class GatewayNode(Node):
    """Base class for external channel gateways."""

    async def on_init(self, bus: BusHandle):
        # Start external listener (websocket, polling, etc.)
        self._external_task = asyncio.create_task(self._listen_external())

    async def _listen_external(self):
        """Override: receive from external channel, publish to /inbound."""
        raise NotImplementedError

    async def on_message(self, topic: str, msg: Message):
        """Receives from /outbound, sends to external channel."""
        if topic == "/outbound":
            await self._send_external(msg)

    async def _send_external(self, msg: Message):
        """Override: send message to external channel."""
        raise NotImplementedError
```

A Slack gateway would look like:

```python
class SlackGateway(GatewayNode):
    name = "gateway-slack"
    subscriptions = ["/outbound"]
    publications = ["/inbound"]

    async def _listen_external(self):
        async for event in self.slack_client.events():
            await self.publish("/inbound", InboundChat(
                channel="slack",
                sender=event.user,
                text=event.text,
                metadata={"thread_ts": event.thread_ts}
            ))

    async def _send_external(self, msg: Message):
        await self.slack_client.post(
            channel=msg.payload.reply_to,
            text=msg.payload.text
        )
```

The key invariant: **PlannerNode never changes.** It subscribes to `/inbound` regardless of whether messages come from a CLI, Slack, Discord, or a test harness. The gateway is just another node.

---

## 9. Package structure

```
agentbus/
├── __init__.py              # public API: MessageBus, Topic, Node, Message, Harness
├── bus.py                   # MessageBus implementation
├── topic.py                 # Topic[T], TopicInfo, retention buffer
├── node.py                  # Node base class, NodeHandle, BusHandle
├── message.py               # Message[T] envelope
├── introspection.py         # TopicInfo, NodeInfo, BusGraph, metrics
├── cli.py                   # agentbus CLI (topic, node, graph commands)
├── launch.py                # YAML launcher
├── gateway.py               # GatewayNode base class (v2 prep)
├── harness/                 # Pi-inspired LLM harness layer
│   ├── __init__.py          # public API: Harness, Extension, Session
│   ├── loop.py              # agent loop: LLM → tool → LLM → respond
│   ├── session.py           # Session, ConversationTurn, branching, persistence
│   ├── compaction.py        # context window summarization
│   ├── extensions.py        # Extension protocol, pipeline runner
│   └── providers/           # LLM provider abstraction
│       ├── __init__.py      # Provider protocol, provider registry
│       ├── ollama.py        # Ollama API provider
│       ├── mlx.py           # Direct MLX inference (Apple Silicon)
│       ├── anthropic.py     # Claude API
│       └── openai.py        # OpenAI + compatible endpoints
├── schemas/                 # built-in Pydantic schemas
│   ├── system.py            # LifecycleEvent, Heartbeat, BackpressureEvent
│   ├── common.py            # InboundChat, OutboundChat, ToolRequest, ToolResult
│   └── harness.py           # ToolCall, ToolResult, PlannerStatus, Session schemas
└── tools/                   # example tool nodes (not core)
    ├── browser.py
    └── code.py
```

Target: `pip install agentbus` gives you bus + harness + providers. Under 3,000 LOC for v1 core.

---

## 10. Non-goals for v1

- **Distribution.** No multi-machine, no network transport. In-process asyncio only.
- **Persistence.** Bus message history lives in memory. No WAL, no replay from disk after restart. (Session persistence to JSON files is in scope — that's the harness layer.)
- **Auth / access control.** Single-user, local-only. No topic-level permissions.
- **GUI dashboard.** CLI-only introspection. A TUI (textual/rich) is a v2 candidate.
- **Gateway implementations.** The `GatewayNode` base class ships, but no concrete Slack/Discord/etc. implementations. That's a v2 concern or a community contrib.
- **Streaming LLM output.** `on_message` returns when processing is complete. Token-by-token streaming within a node is the node's internal concern; the bus sees a single result message.
- **Sub-agents / plan mode / DAGs.** Following Pi's philosophy: the harness is a minimal loop. Planning, sub-agent orchestration, and DAG execution are extension or multi-node concerns, not built into the harness core.
- **Built-in tools.** The harness ships zero tools. Tools are ToolNodes on the bus. Example tool nodes are provided but are not part of the core package.

---

## 11. Robustness patterns (Claude Code-informed)

These are concrete patterns drawn from Claude Code's production architecture — proven failure modes with proven fixes. Each is an easy win for AgentBus v1.

### 11.1 Circuit breakers on every retry loop

Claude Code's most expensive bug: 1,279 sessions hit 50+ consecutive autocompact failures (worst case: 3,272 retries in a single session), burning ~250K wasted API calls/day globally. The fix was three lines: `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3`.

**AgentBus rule:** Every operation that can fail and retry MUST have a circuit breaker with a named constant. No implicit infinite loops. When the breaker trips, the system surfaces an error rather than silently burning resources.

Apply to:

- **Compaction:** `MAX_CONSECUTIVE_COMPACT_FAILURES = 3`. If compaction fails 3 times, the context is irrecoverably over-limit. Stop. Publish a `CompactionFailed` event to `/system/lifecycle` and let the user decide.
- **Tool execution:** `MAX_CONSECUTIVE_TOOL_FAILURES = 5`. A ToolNode that fails 5 times in a row on the same correlation chain is broken. Publish error, don't retry.
- **Provider calls:** `MAX_CONSECUTIVE_PROVIDER_FAILURES = 3` per provider before falling to next in the chain.
- **Queue backpressure:** Already handled in §4.4 (drop-oldest), but add a `MAX_DROPPED_MESSAGES_PER_MINUTE = 50` breaker that pauses the publisher if a subscriber is hopelessly behind.

```python
@dataclass
class CircuitBreaker:
    name: str
    max_failures: int
    consecutive_failures: int = 0

    def record_failure(self) -> bool:
        """Returns True if breaker is now open (should stop)."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_failures:
            return True  # OPEN — stop retrying
        return False

    def record_success(self):
        self.consecutive_failures = 0

    @property
    def is_open(self) -> bool:
        return self.consecutive_failures >= self.max_failures
```

Every circuit breaker publishes to `/system/lifecycle` when it trips. The ObserverNode sees it. The CLI shows it. No silent failures.

### 11.2 Three-layer compaction

Claude Code uses three compression tiers, each triggered at a different cost/impact point. This is the right model for AgentBus's harness:

**MicroCompact** — zero API cost. Runs locally inside the harness after every tool result. Trims old tool outputs (stale file contents, superseded search results) by replacing them with a one-line placeholder: `[tool output truncated: 4,231 tokens → 50 tokens]`. No LLM call, no latency. Runs on every turn.

**AutoCompact** — triggered when context usage exceeds `context_window - AUTOCOMPACT_BUFFER_TOKENS` (default: 13,000 token buffer). Calls the LLM with a compaction prompt to generate a structured summary of up to `MAX_SUMMARY_TOKENS` (default: 20,000). The buffer exists so the summary itself doesn't overflow. Circuit breaker: 3 consecutive failures.

**Full Compact** — user-triggered or on session resume. Compresses the entire conversation, then selectively re-injects: recently accessed files (capped at 5,000 tokens per file), active task context, and relevant skill/tool schemas. Post-compression working budget resets to 50,000 tokens.

```python
# harness/compaction.py constants
AUTOCOMPACT_BUFFER_TOKENS = 13_000
MAX_SUMMARY_TOKENS = 20_000        # p99.99 observed: ~17,400
FILE_REINJECT_CAP_TOKENS = 5_000   # per file
POST_COMPACT_BUDGET_TOKENS = 50_000
```

### 11.3 Streaming fallback with model demotion

Claude Code handles overloaded models with a two-level fallback:

1. **Streaming → non-streaming fallback.** On streaming failure (HTTP 529 overloaded), retry the same request in non-streaming mode. Only foreground operations (user-facing queries) get this retry — background operations (compaction, memory consolidation) do NOT fall back, to avoid cascading model switches during maintenance.

2. **Model demotion.** After 3 consecutive 529 errors on the primary model, demote to the next model in the fallback chain. This is a separate loop from the retry logic — the retry loop handles transient errors, the demotion loop handles sustained overload.

**AgentBus translation:** The harness provider chain already supports fallback (§7.6). Add the streaming/non-streaming distinction and the foreground/background split:

```python
class ProviderCall:
    source: Literal["foreground", "background"]
    allow_streaming_fallback: bool   # True for foreground only
    allow_model_demotion: bool       # True for foreground only
```

Background harness operations (compaction, memory consolidation) fail fast and surface errors rather than attempting expensive fallbacks. This prevents a compaction failure from triggering a model switch that affects user-facing latency.

### 11.4 Dependency injection for testability

Claude Code's QueryEngine uses a narrow `QueryDeps` type with exactly 4 injected dependencies:

```typescript
type QueryDeps = {
    callModel: typeof queryModelWithStreaming
    microcompact: typeof microcompactMessages
    autocompact: typeof autoCompactIfNeeded
    uuid: () => string
}
```

Production wires real implementations via `productionDeps()`. Tests inject fakes directly — no monkey-patching, no `unittest.mock.patch` spaghetti.

**AgentBus translation:** The harness loop should accept a `HarnessDeps` protocol:

```python
class HarnessDeps(Protocol):
    async def call_provider(self, messages, tools, **kwargs) -> AsyncIterator[Chunk]: ...
    async def microcompact(self, messages: list[ConversationTurn]) -> list[ConversationTurn]: ...
    async def autocompact(self, messages: list[ConversationTurn]) -> CompactResult: ...
    def uuid(self) -> str: ...
```

Default: `production_deps()` wires the real provider, real compaction, real `uuid4`. Tests inject fakes. The scope is intentionally narrow — 4 dependencies — to validate the pattern before expanding.

This also means `spin_once()` tests (§4.3) compose cleanly: inject a fake provider that returns a canned tool call, assert the bus routes it correctly.

### 11.5 Prompt cache boundary

Claude Code splits the system prompt at `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`. Everything before it (instructions, tool schemas) is static and cached globally across all sessions. Everything after it (user's CLAUDE.md, git status, current date) is session-specific and never busts the cache.

**AgentBus translation:** The harness's system prompt assembly should enforce the same split:

```python
class SystemPrompt:
    static_prefix: str      # tool schemas, base instructions — cacheable
    dynamic_suffix: str     # session context, user prefs, timestamp — per-call

    def render(self) -> list[dict]:
        return [
            {"type": "text", "text": self.static_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": self.dynamic_suffix},
        ]
```

For local models (Ollama/MLX) this doesn't matter yet. For API providers (Anthropic), this is significant cost savings — the static prefix gets prompt-cached across calls within a session.

### 11.6 Concurrency safety

Claude Code enforces serial writes via an `isConcurrencySafe` gate. Parallel tool execution is fine for reads, but writes (file edits, bash commands that mutate state) must be serialized to prevent corruption.

**AgentBus translation:** ToolNodes should declare a `concurrency_mode`:

```python
class ToolNode(Node):
    concurrency_mode: Literal["parallel", "serial"] = "parallel"
```

The bus respects this: parallel ToolNodes process multiple `/tools/request` messages concurrently. Serial ToolNodes use a semaphore of 1 — requests queue up. This is cleaner than leaving concurrency control to individual tool implementations.

Default to `serial` for anything that mutates filesystem or system state (bash, file write). Default to `parallel` for reads (file read, web fetch, memory query).

### 11.7 Operational telemetry signals

Claude Code tracks two signals that most agent frameworks miss:

- **Frustration metric:** Profanity frequency as a leading UX indicator. If users are cursing, something is breaking down.
- **Stall counter:** How often the user types "continue" mid-session. A proxy for moments where the agent lost momentum and the human had to nudge it.

**AgentBus translation:** The harness should track and publish these to `/system/telemetry`:

```python
class TelemetryEvent(BaseModel):
    event: Literal[
        "stall_detected",         # user re-prompted without new info
        "context_pressure",       # >80% context window used
        "tool_timeout",           # tool exceeded timeout
        "model_demotion",         # fallback chain activated
        "compact_triggered",      # any compaction tier fired
        "breaker_tripped",        # any circuit breaker opened
    ]
    detail: str
    session_id: str
    timestamp: datetime
```

The ObserverNode aggregates these. Over time, the telemetry reveals which tools are slow, which sessions hit context pressure, and where the agent stalls — the same operational visibility Claude Code gets from BigQuery, but local-first via the bus.

---

## 12. Success criteria

- A user can define a planner + two tools + memory node in under 50 lines of application code (excluding the bus/topic/node framework itself).
- A harness-powered PlannerNode (with provider, session, and tool bridge) can be configured in under 30 lines.
- Swapping from Ollama to MLX to Anthropic requires changing one config field, zero code changes.
- `agentbus topic echo /tools/request` shows live message flow within 1 second of a message being published.
- `agentbus topic echo /planning/status` shows harness state transitions (thinking, tool_dispatched, compacting, responding) in real time.
- `agentbus graph --format mermaid` produces a correct topology diagram from a running bus.
- Adding a new tool node requires zero changes to any existing node.
- Adding a gateway node (v2) requires zero changes to any existing node.
- An extension can block a tool call, transform a response, or inject RAG context — each in under 10 lines.
- Session branching preserves full history: forking at turn 5, exploring two tool chains, and switching back loses nothing.
- Overhead of the bus itself (routing, schema validation, introspection bookkeeping) is under 1ms per message on M4 hardware.