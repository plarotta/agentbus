# Bus Layer Reference

The bus layer is the core pub/sub infrastructure. It has zero LLM-specific
code — it is equally useful for data pipelines, event systems, and agent
orchestration.

---

## Message[T]

```python
from agentbus import Message
from agentbus.message import Message  # same thing
```

`Message` is a frozen Pydantic model. Nodes never construct it — the bus
builds the envelope at `publish()` time.

```python
class Message(BaseModel, Generic[T]):
    id: str               # UUID4, auto-generated
    timestamp: datetime   # UTC, set at publish time
    source_node: str      # set by bus — never forged by nodes
    topic: str            # the topic name
    correlation_id: str | None = None
    payload: T            # the typed payload
```

**Reading payload type at runtime:**

```python
async def on_message(self, msg: Message) -> None:
    # msg.payload is the raw Python object
    if isinstance(msg.payload, InboundChat):
        text = msg.payload.text
```

---

## Topic[T]

```python
from agentbus import Topic
```

### Parameterization

```python
topic = Topic[InboundChat]("/inbound", retention=10)
topic.schema   # → InboundChat
topic.name     # → "/inbound"
```

`Topic.__class_getitem__` creates a subclass with `_schema` set at class
creation time. All constructor arguments are forwarded normally.

### Constructor

```python
Topic[SchemaType](
    name: str,
    *,
    retention: int = 0,
    description: str = "",
    backpressure_policy: Literal["drop-oldest", "drop-newest"] = "drop-oldest",
)
```

| Parameter | Description |
|-----------|-------------|
| `name` | Topic path, e.g. `"/tools/request"`. By convention: lowercase, slash-delimited. |
| `retention` | Number of recent messages to retain in the buffer. `0` disables retention. |
| `description` | Human-readable description shown in `agentbus topic list`. |
| `backpressure_policy` | What to do when a subscriber's queue is full. `"drop-oldest"` discards the oldest queued message; `"drop-newest"` discards the incoming message. |

### Wildcard matching

The `matches(pattern)` method tests whether a subscription pattern covers this
topic. The bus calls this when wiring nodes to topics.

```python
t = Topic[InboundChat]("/system/lifecycle")
t.matches("/system/*")    # True — one segment
t.matches("/system/**")   # True — zero or more segments
t.matches("/system/*/x")  # False — extra segment
```

### Retention buffer

```python
# After publishing several messages:
messages = bus.history("/inbound", n=5)  # last 5 messages
messages = bus.history("/inbound")       # last 10 (default)
```

`retention=0` means history is not kept. Topics with `retention=0` always
return an empty list from `bus.history()`.

### Backpressure

When a subscriber's queue (default size: 100) is full, the configured policy
applies and a `BackpressureEvent` is published to `/system/backpressure`:

```python
class BackpressureEvent(BaseModel):
    topic: str
    subscriber_node: str
    queue_size: int
    dropped_message_id: str
    policy: Literal["drop-oldest", "drop-newest"]
```

---

## Node

```python
from agentbus import Node
```

### Class attributes

```python
class MyNode(Node):
    name: ClassVar[str]                              # required, unique
    subscriptions: ClassVar[list[str]] = []
    publications: ClassVar[list[str]] = []
    concurrency: ClassVar[int] = 1
    concurrency_mode: ClassVar[Literal["parallel", "serial"]] = "parallel"
```

| Attribute | Description |
|-----------|-------------|
| `name` | Unique identifier. Used in introspection, logs, and `source_node`. |
| `subscriptions` | Topic name patterns this node receives. Wildcards allowed. |
| `publications` | Topic name patterns this node may publish to. Wildcards allowed. Must be a superset of topics actually published to, or the bus raises `UndeclaredPublicationError`. |
| `concurrency` | Maximum concurrent `on_message` calls in `"parallel"` mode. Ignored in `"serial"` mode. |
| `concurrency_mode` | `"serial"` enforces one `on_message` at a time. `"parallel"` allows up to `concurrency` concurrent calls. |

### Lifecycle hooks

All hooks default to no-ops. Override only what you need.

```python
async def on_init(self, bus: BusHandle) -> None:
    """Called once before spin begins. Store the bus handle here."""

async def on_message(self, msg: Message) -> None:
    """Called for each delivered message. msg.topic tells you which topic."""

async def on_shutdown(self) -> None:
    """Called during the shutdown phase. Cancel tasks, close connections."""
```

**Best practice:** store the bus handle in `on_init` and use it in
`on_message`. Do not hold a reference to `MessageBus` itself.

```python
class MyNode(Node):
    name = "my_node"
    subscriptions = ["/inbound"]
    publications = ["/outbound"]

    def __init__(self):
        self._bus = None

    async def on_init(self, bus: BusHandle) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        await self._bus.publish("/outbound", OutboundChat(text=msg.payload.text))
```

### NodeState

```python
from agentbus.node import NodeState

NodeState.CREATED   # initial state, before on_init
NodeState.RUNNING   # after on_init completes successfully
NodeState.STOPPED   # after on_shutdown completes
NodeState.ERROR     # circuit breaker tripped
```

---

## BusHandle

`BusHandle` is the capability interface given to nodes in `on_init`. It is a
`@runtime_checkable` Protocol — you can `isinstance(bus, BusHandle)` in tests.

### `publish`

```python
async def publish(
    self,
    topic: str,
    payload: Any,
    correlation_id: str | None = None,
) -> None
```

Publishes `payload` to `topic`. Raises `UndeclaredPublicationError` if `topic`
is not in the node's `publications`. Raises `TopicSchemaError` if `payload`
does not match the topic's schema.

```python
await self._bus.publish("/outbound", OutboundChat(text="hello"))
await self._bus.publish("/tools/result", result, correlation_id=msg.correlation_id)
```

### `request`

```python
async def request(
    self,
    topic: str,
    payload: Any,
    reply_on: str,
    *,
    timeout: float = 30.0,
) -> Message
```

Publish `payload` to `topic` with a generated `correlation_id`, then await a
message on `reply_on` that echoes the same `correlation_id`. Returns the reply
`Message`. Raises `RequestTimeoutError` on timeout.

```python
reply = await self._bus.request(
    "/tools/request",
    ToolRequest(tool="search", params={"query": "..."}),
    reply_on="/tools/result",
    timeout=10.0,
)
result: ToolResult = reply.payload
```

The responding node must pass the correlation_id back:
```python
await self._bus.publish("/tools/result", result, correlation_id=msg.correlation_id)
```

### `topic_history`

```python
async def topic_history(self, topic: str, n: int = 10) -> list[Message]
```

Return the last `n` messages from a topic's retention buffer.

---

## MessageBus

```python
from agentbus import MessageBus
```

### Constructor

```python
MessageBus(
    heartbeat_interval: float = 30.0,
    message_log_maxlen: int = 10_000,
    socket_path: str | None = "/tmp/agentbus.sock",
)
```

| Parameter | Description |
|-----------|-------------|
| `heartbeat_interval` | Seconds between `/system/heartbeat` publications. |
| `message_log_maxlen` | Max messages kept in the internal log (used for introspection `echo`). |
| `socket_path` | Unix socket path for the CLI/introspection server. Pass `None` to disable. |

**Testing tip:** Pass `socket_path=None` in tests to avoid port conflicts and
the 104-character macOS path limit.

### Registration

```python
bus.register_topic(Topic[InboundChat]("/inbound", retention=10))
bus.register_node(PlannerNode())
```

`register_topic` raises `DuplicateTopicError` on name collision.

`register_node` raises:
- `DuplicateNodeError` — name already registered
- `UndeclaredSubscriptionError` — node subscribes to a topic that isn't registered

Topic registration must happen before node registration for subscriptions to
wire correctly.

### `spin()`

```python
await bus.spin(
    until: Callable[[], bool] | None = None,
    max_messages: int | None = None,
    timeout: float | None = None,
) -> SpinResult
```

Runs through four lifecycle phases:

1. **VALIDATION** — check topology, log warnings (never raises)
2. **INIT** — call `on_init()` on all nodes in parallel
3. **SPIN** — each node gets its own `asyncio.Task`; runs until termination
4. **SHUTDOWN** — cancel tasks, call `on_shutdown()` on all nodes

**Termination conditions** (checked after each message):
- `until()` returns `True`
- `max_messages` messages processed
- `timeout` seconds elapsed (wall clock)
- No args: run until `asyncio.CancelledError` (e.g. `Ctrl-C`)

`spin()` returns a `SpinResult`:

```python
class SpinResult:
    messages_processed: int
    duration_s: float
    per_node: dict[str, NodeStats]  # {"planner": NodeStats(...)}
    errors: list[str]

class NodeStats:
    messages_received: int
    messages_published: int
    errors: int
```

**Common termination patterns:**

```python
# Stop after all expected outputs arrive in retention buffer:
await bus.spin(
    until=lambda: len(bus.history("/outbound", 10)) >= 3
)

# Stop after N total messages:
await bus.spin(max_messages=50)

# Hard time limit:
await bus.spin(timeout=30.0)

# Combined — whichever triggers first:
await bus.spin(until=lambda: done, timeout=60.0)
```

### `spin_once()`

```python
await bus.spin_once(timeout: float = 5.0) -> Message | None
```

Process exactly one queued message across all nodes. Returns the message, or
`None` if no message arrives within `timeout`. This is the **primary testing
primitive** — use it instead of `spin()` in tests.

```python
bus.publish("/inbound", InboundChat(channel="test", sender="u", text="hi"))
msg = await bus.spin_once()
assert msg.topic == "/inbound"
```

### `publish()`

Direct bus publication (not via a node):

```python
bus.publish(
    topic_name: str,
    payload: Any,
    *,
    source_node: str = "_bus_",
    correlation_id: str | None = None,
) -> Message
```

Use this in tests, startup sequences, and `seed_messages` tasks. This is a
synchronous method — call it directly without `await`.

### Introspection

All introspection methods are synchronous and safe to call at any time:

```python
bus.topics() -> list[TopicInfo]
bus.nodes()  -> list[NodeInfo]
bus.graph()  -> BusGraph
bus.history(topic: str, n: int = 10) -> list[Message]
```

**TopicInfo:**

```python
@dataclass
class TopicInfo:
    name: str
    schema_name: str
    retention: int
    subscriber_count: int
    message_count: int
    queue_depths: dict[str, int]   # per-subscriber queue depths
```

**NodeInfo:**

```python
@dataclass
class NodeInfo:
    name: str
    state: str             # "CREATED" | "RUNNING" | "STOPPED" | "ERROR"
    concurrency: int
    concurrency_mode: str  # "parallel" | "serial"
    subscriptions: list[str]
    publications: list[str]
    messages_received: int
    messages_published: int
    errors: int
```

**BusGraph:**

```python
@dataclass
class BusGraph:
    nodes: list[NodeInfo]
    topics: list[TopicInfo]
    edges: list[Edge]

@dataclass
class Edge:
    node: str
    topic: str
    direction: Literal["sub", "pub"]
```

**Async introspection:**

```python
# Block until a specific message arrives:
msg = await bus.wait_for(
    "/outbound",
    predicate=lambda m: m.payload.reply_to == "user",
    timeout=30.0,
)

# Tap a live topic stream:
async for msg in bus.echo("/tools/request", n=10):
    print(msg.payload)
```

---

## System topics

The bus auto-registers these topics. Nodes must never publish to them directly.

| Topic | Schema | Retention | When published |
|-------|--------|-----------|----------------|
| `/system/lifecycle` | `LifecycleEvent` | 100 | Node state transitions |
| `/system/heartbeat` | `Heartbeat` | 1 | Every `heartbeat_interval` seconds |
| `/system/backpressure` | `BackpressureEvent` | 0 | When a queue drops a message |
| `/system/telemetry` | `TelemetryEvent` | 50 | Harness operational events |

**LifecycleEvent:**

```python
class LifecycleEvent(BaseModel):
    node: str
    event: Literal["started", "stopped", "error", "init_failed"]
    error: str | None = None
    timestamp: datetime
```

**Heartbeat:**

```python
class Heartbeat(BaseModel):
    uptime_s: float
    node_count: int
    topic_count: int
    total_messages: int
    messages_per_second: float   # rolling 60s window
    node_states: dict[str, str]  # {"planner": "RUNNING", ...}
    queue_depths: dict[str, int] # {"planner": 0, "browser": 3}
```

Subscribe to `/system/*` with `ObserverNode` to get all of these.

---

## ObserverNode

```python
from agentbus import ObserverNode
```

`ObserverNode` subscribes to `/system/*` and logs every lifecycle event,
backpressure event, and telemetry event. Add it to any bus for free
structured logging:

```python
bus.register_node(ObserverNode())
```

It stores received events in `observer.events: list[Message]` for inspection
after `spin()`.

---

## GatewayNode

```python
from agentbus import GatewayNode
```

Abstract base class for bridging external I/O into the bus. Subclass it to
connect HTTP servers, message queues, stdin, WebSockets, etc.

```python
class MyGateway(GatewayNode):
    name = "http_gateway"

    async def _listen_external(self) -> None:
        """Called as asyncio.Task during on_init. Read external input here."""
        async for event in external_source():
            await self.publish_external(
                InboundChat(channel="http", sender=event.user, text=event.text)
            )

    async def _send_external(self, msg: Message) -> None:
        """Called for each /outbound message. Send it out here."""
        await http_client.send(msg.payload.text, to=msg.payload.reply_to)
```

`GatewayNode` hardcodes `subscriptions = ["/outbound"]` and
`publications = ["/inbound"]`. Override these class attributes if your gateway
uses different topic names.

---

## Error reference

All errors inherit from `AgentBusError`:

| Exception | When raised |
|-----------|-------------|
| `TopicSchemaError` | Payload type doesn't match topic's schema |
| `UndeclaredPublicationError` | Node published to a topic not in its `publications` |
| `UndeclaredSubscriptionError` | Node subscribes to a topic not registered on the bus |
| `DuplicateNodeError` | `register_node` called with a name already in use |
| `DuplicateTopicError` | `register_topic` called with a name already in use |
| `RequestTimeoutError` | `bus.request()` or `bus.wait_for()` timed out |
| `NodeInitError` | `on_init()` raised an exception |
| `CircuitBreakerOpenError` | Operation rejected because breaker is open |

```python
from agentbus.errors import AgentBusError, TopicSchemaError, RequestTimeoutError
```

---

## Testing patterns

### Use `spin_once()` not `spin()`

```python
async def test_echo_node(tmp_path):
    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[InboundChat]("/inbound", retention=5))
    bus.register_topic(Topic[OutboundChat]("/outbound", retention=5))
    bus.register_node(EchoNode())
    await bus._init_phase()

    bus.publish("/inbound", InboundChat(channel="t", sender="u", text="hello"))
    await bus.spin_once()

    history = bus.history("/outbound")
    assert history[0].payload.text == "olleh"
```

### Use `until=` for integration tests

```python
async def test_pipeline(tmp_path):
    bus = MessageBus(socket_path=None)
    # ... register topics and nodes ...

    bus.publish("/inbound", InboundChat(...))
    await bus.spin(
        until=lambda: len(bus.history("/outbound", 5)) >= 1,
        timeout=5.0,
    )

    assert bus.history("/outbound")[0].payload.text == "expected"
```

### macOS socket path limit

macOS `AF_UNIX` paths are limited to 104 characters. Tests that use sockets
must use short paths:

```python
import tempfile, shutil

@pytest.fixture
def short_tmp():
    d = tempfile.mkdtemp(dir="/tmp", prefix="ab_")
    yield d
    shutil.rmtree(d, ignore_errors=True)
```
