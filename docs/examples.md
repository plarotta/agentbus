# Examples

All examples are in the `examples/` directory and can be run directly with `uv`.

---

## echo_agent — Bus basics without an LLM

`examples/echo_agent/main.py`

The simplest possible bus example. `EchoNode` subscribes to `/inbound` and
publishes reversed text to `/outbound`.

```bash
uv run python examples/echo_agent/main.py
```

**What it demonstrates:**
- Registering topics and nodes
- `on_init` / `on_message` lifecycle
- Publishing from within a node via `BusHandle`
- Stopping `spin()` using `until=` with `bus.history()`

**Key patterns:**

```python
class EchoNode(Node):
    name = "echo"
    subscriptions = ["/inbound"]
    publications = ["/outbound"]

    async def on_init(self, bus) -> None:
        self._bus = bus          # store handle for use in on_message

    async def on_message(self, msg: Message) -> None:
        await self._bus.publish(
            "/outbound",
            OutboundChat(text=msg.payload.text[::-1], reply_to=msg.source_node),
        )
```

**Termination pattern:**

```python
# Stop when 3 messages have been published to /outbound.
# history() reads from the retention buffer — no node subscription needed.
await bus.spin(until=lambda: len(bus.history("/outbound", 10)) >= 3)
```

---

## sensor_pipeline — Multi-stage stream processing

`examples/sensor_pipeline/main.py`

A four-node data processing chain. No LLM required.

```bash
uv run python examples/sensor_pipeline/main.py
```

```
Streaming 60 readings  (warn >=74.0  crit >=82.0)

  stats   [####----------------]  mean= 63.0  stdev=4.41
  [ WARN ]  sensor-A  mean=74.8
  stats   [############--------]  mean= 79.8  stdev=2.97

──────────────────────────────────────────────────
  readings  : 60
  temp range: 54.2 – 84.8  (mean 71.3)
  alerts    : 1
    warning    at mean=74.8
```

**Architecture:**

```
SensorNode → /readings → StatsNode → /stats → AlertNode → /alerts
                                                           ↓
                                                      DisplayNode
```

**What it demonstrates:**
- Multi-stage typed message routing
- Stateful nodes (rolling window in `StatsNode`)
- Conditional publishing — `AlertNode` only publishes on level *change*, not on
  every stats update
- `asyncio.create_task()` inside `on_init` for background emission
- Post-run analysis from retention buffers

**Stateful node pattern:**

```python
class StatsNode(Node):
    def __init__(self) -> None:
        self._windows: dict[str, deque] = {}

    async def on_message(self, msg: Message) -> None:
        r: SensorReading = msg.payload
        window = self._windows.setdefault(r.sensor_id, deque(maxlen=WINDOW_SIZE))
        window.append(r.value)
        # only publish stats once we have enough data points
        if len(window) < 3:
            return
        await self._bus.publish("/stats", WindowStats(...))
```

**Conditional publish pattern:**

```python
class AlertNode(Node):
    def __init__(self) -> None:
        self._prev_level: dict[str, str] = {}

    async def on_message(self, msg: Message) -> None:
        # ... compute level ...
        if level == self._prev_level.get(sensor_id, "ok"):
            return  # no change — don't flood /alerts
        self._prev_level[sensor_id] = level
        await self._bus.publish("/alerts", Alert(...))
```

---

## tool_agent — Standalone Harness with tools

`examples/tool_agent/main.py`

The simplest LLM example. The `Harness` is used directly, with no bus. Good
starting point for understanding the tool-call loop.

**Setup:** `uv sync --extra anthropic`

```bash
ANTHROPIC_API_KEY=sk-ant-... uv run python examples/tool_agent/main.py
```

```
User: What is 2 raised to the power of 32?
Assistant: 2 raised to the power of 32 is 4,294,967,296.

User: What is the square root of that result?
Assistant: The square root of 4,294,967,296 is 65,536.

Session saved → ~/.agentbus/sessions/abc123/main.json
Total turns:    16
Total tokens:   124
```

**What it demonstrates:**
- Defining `ToolSchema` with JSON Schema `input_schema`
- Implementing a `tool_executor` callback (async)
- Multi-turn conversation via a single `Session`
- Context preserved across calls — Q2 uses the result of Q1 without the user
  having to repeat it
- Session saved to disk automatically

**Tool definition pattern:**

```python
TOOLS = [
    ToolSchema(
        name="calculate",
        description="Evaluate a mathematical expression.",
        input_schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string"},
            },
            "required": ["expression"],
        },
    ),
]

async def execute_tool(call: ToolCall) -> ToolResult:
    match call.name:
        case "calculate":
            result = eval(call.arguments["expression"], {"__builtins__": {}}, {"math": math})
            return ToolResult(tool_call_id=call.id, output=str(result))
        case _:
            return ToolResult(tool_call_id=call.id, error=f"unknown tool: {call.name}")
```

---

## bus_agent — Harness wired into the bus

`examples/bus_agent/main.py`

The canonical bus + LLM integration pattern. Tool calls from the `Harness` are
routed through the bus as typed messages. `PlannerNode` and `ToolExecutorNode`
never import each other.

**Setup:** `uv sync --extra anthropic`

```bash
ANTHROPIC_API_KEY=sk-ant-... uv run python examples/bus_agent/main.py
```

**Architecture:**

```
/inbound → PlannerNode (Harness inside)
                │
                ▼ bus.request()
          /tools/request → ToolExecutorNode
                               │
                        /tools/result ←
                               │
                          (future resolves)
                               │
                     PlannerNode continues loop
                               │
                               ▼
                          /outbound → OutputNode
```

**What it demonstrates:**
- `bus.request()` / reply pattern with `correlation_id`
- `PlannerNode` as a stateful serial node containing a full `Harness`
- `ToolExecutorNode` as a pure tool implementation with no LLM code
- Sequential questions using `bus.wait_for()` to gate Q2 on Q1's response
- `until=` termination using `bus.history()`

**Request/reply pattern:**

```python
# In PlannerNode's tool_executor callback:
async def tool_executor(call: ToolCall) -> HarnessToolResult:
    reply: Message = await self._bus.request(
        "/tools/request",
        BusToolRequest(tool=call.name, params=call.arguments),
        reply_on="/tools/result",
        timeout=30.0,
    )
    result: BusToolResult = reply.payload
    return HarnessToolResult(
        tool_call_id=call.id,
        output=result.output,
        error=result.error,
    )
```

```python
# In ToolExecutorNode — echo back correlation_id so request() resolves:
async def on_message(self, msg: Message) -> None:
    output = await _run_tool(msg.payload.tool, msg.payload.params)
    await self._bus.publish(
        "/tools/result",
        BusToolResult(tool_call_id=msg.id, output=output),
        correlation_id=msg.correlation_id,  # required!
    )
```

**Sequential seeding with wait_for:**

```python
async def seed_messages() -> None:
    await asyncio.sleep(0.1)
    for text in questions:
        bus.publish("/inbound", InboundChat(...))
        # block until this question's response arrives before sending the next
        await bus.wait_for("/outbound", lambda m: True, timeout=120.0)
```

---

## writer_critic — Multi-agent collaboration

`examples/writer_critic/main.py`

Two LLM agents with different system prompts collaborate via the bus. Neither
imports the other. The bus routes their messages.

**Setup:** `uv sync --extra anthropic`

```bash
ANTHROPIC_API_KEY=sk-ant-... uv run python examples/writer_critic/main.py

# Configure with env vars:
ROUNDS=3 TOPIC="the joy of refactoring" MODEL=claude-opus-4-6 \
  uv run python examples/writer_critic/main.py
```

```
[Writer] Round 1 — writing about: why it's worth learning to type properly
  ...

[Critic] Reviewing round 1 draft
  ...

[Writer] Round 2 — revising
  ...

============================================================
  FINAL PIECE  (2 round(s) of revision)
============================================================
  ...

  writer      turns=8   tokens=312
  critic      turns=6   tokens=248
```

**Architecture:**

```
/tasks → WriterNode (Harness + writer persona)
              │
              ▼
         /drafts → CriticNode (Harness + critic persona)
              │          │
          /feedback ←────┘ (unless is_final)
              │
              ▼ (when is_final)
         /final → OutputNode
```

**What it demonstrates:**
- Two independent `Harness` instances with different `SystemPrompt` personas
- Each agent has its own `Session` — independent conversation histories
- Typed schemas (`Draft`, `Critique`, `FinalPiece`) as the contract between agents
- `concurrency_mode = "serial"` for both agents (stateful LLM loops)
- Iteration control: `Draft.max_rounds` signals when to stop
- Post-run session stats from `harness.session`

**Multi-agent persona pattern:**

```python
class WriterNode(Node):
    async def on_init(self, bus):
        self._harness = Harness(
            provider=AnthropicProvider(
                model=MODEL,
                system_prompt=SystemPrompt(
                    static_prefix="You are a creative writer. ..."
                ),
            ),
            tool_executor=_no_tools,
            tools=[],
            session=Session(),
        )

class CriticNode(Node):
    async def on_init(self, bus):
        self._harness = Harness(
            provider=AnthropicProvider(
                model=MODEL,
                system_prompt=SystemPrompt(
                    static_prefix="You are a sharp, constructive editor. ..."
                ),
            ),
            tool_executor=_no_tools,
            tools=[],
            session=Session(),
        )
```

**Observing live traffic** (while the example runs):

```bash
# In another terminal:
agentbus topic echo /drafts
agentbus topic echo /feedback
agentbus node list
```

---

## Common patterns across examples

### Stopping cleanly

Always use `until=` or `timeout=` — never rely on `max_messages` unless you
know the exact count:

```python
# Preferred: condition-based
await bus.spin(
    until=lambda: len(bus.history("/outbound", n + 1)) >= n,
    timeout=60.0,
)

# Also good for fixed pipelines:
await bus.spin(max_messages=expected_count, timeout=30.0)
```

### Seeding messages after spin starts

Messages published before nodes are initialized may be dropped (nodes don't
have queues yet). Always seed after a brief delay or inside `on_init`:

```python
async def seed() -> None:
    await asyncio.sleep(0.1)  # let nodes init
    bus.publish("/inbound", ...)

asyncio.create_task(seed())
await bus.spin(...)
```

### Testing with FakeDeps

For unit tests, swap out the real provider with `FakeDeps`:

```python
from agentbus.harness.providers import Chunk

class FakeDeps:
    def __init__(self, turns):
        self.turns = list(turns)

    async def call_provider(self, messages, tools, **kwargs):
        chunks = self.turns.pop(0)
        async def _stream():
            for chunk in chunks:
                yield chunk
        return _stream()

    async def microcompact(self, messages): return messages
    async def autocompact(self, messages):
        from agentbus.harness.compaction import CompactResult
        return CompactResult(messages=messages, compacted=False)
    def uuid(self): return "test-id"

# Test:
deps = FakeDeps([[Chunk(text="hello")]])
harness = Harness(deps=deps, tool_executor=executor, tools=[], session=Session())
assert await harness.run("hi") == "hello"
```
