# Schema Reference

AgentBus ships three schema modules. All schemas are Pydantic v2 `BaseModel`
subclasses.

---

## agentbus.schemas.harness

Used internally by the `Harness` and `Session`. Import as:

```python
from agentbus.schemas.harness import ToolCall, ToolResult, ConversationTurn
```

### ToolCall

A tool invocation produced by the LLM during the agent loop.

```python
class ToolCall(BaseModel):
    id: str               # provider-assigned ID, e.g. "toolu_01XS..."
    name: str             # tool name from ToolSchema
    arguments: dict = {}  # parsed JSON arguments
```

### ToolResult

The result returned by `tool_executor` after running a tool. Either `output`
or `error` should be set, not both.

```python
class ToolResult(BaseModel):
    tool_call_id: str           # must match the ToolCall.id
    output: str | None = None   # tool output text
    error: str | None = None    # error message if the tool failed
```

> **Note:** `ToolResult` also exists in `agentbus.schemas.common` (the
> bus-facing version). When importing both, alias one to avoid name conflicts:
> ```python
> from agentbus.schemas.harness import ToolResult as HarnessToolResult
> from agentbus.schemas.common import ToolResult as BusToolResult
> ```

### ContentBlock

A single block in a multi-part message (for future image/binary support).

```python
class ContentBlock(BaseModel):
    type: str
    text: str | None = None
    data: Any | None = None
```

### ConversationTurn

A single turn in the session conversation history.

```python
class ConversationTurn(BaseModel):
    role: Literal["user", "assistant", "tool_result"]
    content: str | list[ContentBlock]
    tool_calls: list[ToolCall] | None = None  # set on assistant turns with tool invocations
    tool_call_id: str | None = None           # set on tool_result turns; links to ToolCall.id
    token_count: int = 0
    timestamp: datetime                        # UTC, auto-set
```

**Role semantics:**

| Role | When used | Notable fields |
|------|-----------|----------------|
| `"user"` | User input, injected by `harness.run()` | `content` = user text |
| `"assistant"` | LLM response | `tool_calls` = list if LLM called tools; `content` = text if LLM responded directly |
| `"tool_result"` | Tool execution output | `tool_call_id` = matching `ToolCall.id`; `content` = tool output or error |

A complete tool-use cycle in session history looks like:

```
role=user,         content="What is 2^32?"
role=assistant,    content="",  tool_calls=[{id: "id1", name: "calculate", ...}]
role=tool_result,  content="4294967296",  tool_call_id="id1"
role=assistant,    content="2^32 is 4,294,967,296."
```

### PlannerStatus

Published to `/planning/status` by a `PlannerNode` to expose harness state.
Allows `agentbus topic echo /planning/status` to show LLM state transitions in
real time.

```python
class PlannerStatus(BaseModel):
    event: Literal[
        "thinking",        # LLM call in progress
        "tool_dispatched", # tool call sent to bus
        "tool_received",   # tool result received from bus
        "compacting",      # context compaction triggered
        "responding",      # final response being generated
        "error",           # harness encountered an error
    ]
    iteration: int
    context_tokens: int
    context_capacity: float    # 0.0–1.0, fraction of context window used
    tool_name: str | None = None
    detail: str | None = None
```

---

## agentbus.schemas.common

Bus-facing schemas for standard inter-agent communication patterns. Import as:

```python
from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest, ToolResult
```

### InboundChat

Messages arriving from an external channel (gateway, CLI, test harness).

```python
class InboundChat(BaseModel):
    channel: str              # e.g. "slack", "http", "cli"
    sender: str               # user identifier
    text: str                 # message content
    metadata: dict = {}       # arbitrary extra data
```

### OutboundChat

Messages leaving to an external channel.

```python
class OutboundChat(BaseModel):
    text: str
    reply_to: str | None = None   # sender from the originating InboundChat
    metadata: dict = {}
```

### ToolRequest

Tool execution request published to `/tools/request` by a planner node.

```python
class ToolRequest(BaseModel):
    tool: str                 # tool name
    action: str | None = None # optional sub-action
    params: dict = {}         # tool arguments
```

### ToolResult (common)

Tool execution result published to `/tools/result` by a tool node.

```python
class ToolResult(BaseModel):
    tool_call_id: str
    output: str | None = None
    error: str | None = None
```

Identical fields to `schemas/harness.py:ToolResult`. The distinction is that
this version is the bus-facing payload; the harness version is the return type
of `tool_executor`.

---

## agentbus.schemas.system

Published automatically by the bus. Nodes never publish to system topics
directly. Import as:

```python
from agentbus.schemas.system import LifecycleEvent, Heartbeat, BackpressureEvent, TelemetryEvent
```

### LifecycleEvent

Published to `/system/lifecycle` on node state transitions.

```python
class LifecycleEvent(BaseModel):
    node: str
    event: Literal["started", "stopped", "error", "init_failed"]
    error: str | None = None    # set when event == "error" or "init_failed"
    timestamp: datetime
```

### Heartbeat

Published to `/system/heartbeat` every `heartbeat_interval` seconds.

```python
class Heartbeat(BaseModel):
    uptime_s: float
    node_count: int
    topic_count: int
    total_messages: int
    messages_per_second: float         # rolling 60s window
    node_states: dict[str, str]        # {"planner": "RUNNING", ...}
    queue_depths: dict[str, int]       # {"planner": 0, "browser": 3}
```

### BackpressureEvent

Published to `/system/backpressure` when a subscriber queue drops a message.

```python
class BackpressureEvent(BaseModel):
    topic: str
    subscriber_node: str
    queue_size: int
    dropped_message_id: str
    policy: Literal["drop-oldest", "drop-newest"]
```

### TelemetryEvent

Published to `/system/telemetry` by the harness for operational observability.

```python
class TelemetryEvent(BaseModel):
    event: Literal[
        "stall_detected",    # user re-prompted without new info
        "context_pressure",  # >80% context window used
        "tool_timeout",      # tool exceeded timeout
        "model_demotion",    # fallback provider chain activated
        "compact_triggered", # any compaction tier fired
        "breaker_tripped",   # any circuit breaker opened
    ]
    detail: str
    session_id: str
    timestamp: datetime
```

---

## Introspection data classes

These are plain Python dataclasses (not Pydantic). They appear in the output of
`bus.topics()`, `bus.nodes()`, `bus.graph()`, and `bus.spin()`.

```python
from agentbus.introspection import (
    TopicInfo, NodeInfo, BusGraph, Edge, SpinResult, NodeStats
)
```

### TopicInfo

```python
@dataclass
class TopicInfo:
    name: str
    schema_name: str           # e.g. "InboundChat"
    retention: int
    subscriber_count: int
    message_count: int
    queue_depths: dict[str, int]  # per-subscriber current queue depth
```

### NodeInfo

```python
@dataclass
class NodeInfo:
    name: str
    state: str                 # "CREATED" | "RUNNING" | "STOPPED" | "ERROR"
    concurrency: int
    concurrency_mode: str      # "parallel" | "serial"
    subscriptions: list[str]
    publications: list[str]
    messages_received: int
    messages_published: int
    errors: int
```

### BusGraph / Edge

```python
@dataclass
class Edge:
    node: str
    topic: str
    direction: Literal["sub", "pub"]

@dataclass
class BusGraph:
    nodes: list[NodeInfo]
    topics: list[TopicInfo]
    edges: list[Edge]
```

### SpinResult / NodeStats

```python
@dataclass
class NodeStats:
    messages_received: int = 0
    messages_published: int = 0
    errors: int = 0

@dataclass
class SpinResult:
    messages_processed: int
    duration_s: float
    per_node: dict[str, NodeStats]
    errors: list[str] = field(default_factory=list)
```
