# AgentBus

A ROS-inspired typed message bus for agentic LLM orchestration. Local-first. asyncio-native. Introspection-first.

## The problem

Most agent frameworks are monolithic loops: the orchestrator calls a tool, gets a result, feeds it back, repeats. This works until you need more than one agent, until you want to observe what's happening, or until you need to route messages between components without hardwiring every dependency.

AgentBus separates concerns the way ROS does for robotics: agents communicate through **typed topics**, not direct function calls. The bus owns routing. The harness owns the LLM loop. Neither knows the other's internals.

## Core abstractions

| Abstraction | Role |
|-------------|------|
| `Message[T]` | Frozen envelope set by the bus (id, timestamp, source_node, topic, payload) |
| `Topic[T]` | Typed publish/subscribe channel with retention and backpressure |
| `Node` | Agent component with `on_init`, `on_message`, `on_shutdown` lifecycle hooks |
| `MessageBus` | Broker — registers topics/nodes, drives the spin loop, exposes introspection |
| `Harness` | Self-contained LLM agent loop — tool dispatch, session history, context compaction |

Nodes declare what they publish and subscribe to. The bus enforces schema correctness at runtime. No node can fake another node's identity — `source_node` is always set by the bus.

## Install

```bash
# core
pip install agentbus

# with a specific LLM provider
pip install "agentbus[anthropic]"
pip install "agentbus[openai]"
pip install "agentbus[ollama]"

# with CLI tools
pip install "agentbus[cli]"

# textual TUI for `agentbus chat`
pip install "agentbus[tui]"

# MCP servers (registers stdio MCP tools with the planner)
pip install "agentbus[mcp]"

# multi-channel gateways
pip install "agentbus[slack]"
pip install "agentbus[telegram]"
pip install "agentbus[channels]"   # slack + telegram together

# everything
pip install "agentbus[all]"
```

With [uv](https://github.com/astral-sh/uv) (recommended):

```bash
uv sync --extra anthropic
```

## Examples

Four runnable examples are in `examples/`.

### Sensor monitoring pipeline (`examples/sensor_pipeline/`) — no API key needed

Multi-stage stream processing: `SensorNode → StatsNode → AlertNode → DisplayNode`. Shows typed pub/sub, stateful rolling-window computation, conditional publishing, and post-run analysis from the retention buffer.

```bash
uv run python examples/sensor_pipeline/main.py
```

```
Streaming 60 readings  (warn >=74.0  crit >=82.0)

  stats   [####----------------]  mean= 63.0  stdev=4.41
  [ WARN ]  sensor-A  mean=74.8
  stats   [############--------]  mean= 79.8  stdev=2.97
  ...
  readings  : 60  |  temp range: 54.2 – 84.8  |  alerts: 1
```

### Standalone tool agent (`examples/tool_agent/`) — Anthropic key required

The simplest way to use the Harness — no bus required. The LLM gets tools, makes calls, the executor runs them locally.

```bash
uv sync --extra anthropic
ANTHROPIC_API_KEY=sk-ant-... uv run python examples/tool_agent/main.py
```

### Full bus integration (`examples/bus_agent/`) — Anthropic key required

The Harness wired into pub/sub. Tool calls route through the bus as typed messages — `PlannerNode` never imports or calls `ToolExecutorNode` directly.

```bash
uv sync --extra anthropic
ANTHROPIC_API_KEY=sk-ant-... uv run python examples/bus_agent/main.py
```

While it runs, observe live traffic in another terminal:

```bash
agentbus topic echo /tools/request
agentbus graph --format mermaid
```

### Writer + Critic multi-agent loop (`examples/writer_critic/`) — Anthropic key required

Two LLM agents with different system prompts collaborate via the bus. `WriterNode` produces a draft, `CriticNode` reviews it, `WriterNode` revises — for N rounds. Each agent has its own `Harness` and `Session`.

```bash
uv sync --extra anthropic
ANTHROPIC_API_KEY=sk-ant-... uv run python examples/writer_critic/main.py

# optional overrides
ROUNDS=3 TOPIC="the joy of refactoring" uv run python examples/writer_critic/main.py
```

```
[Writer] Round 1 — writing about: why it's worth learning to type properly
  ...draft...
[Critic] Reviewing round 1 draft
  ...feedback...
[Writer] Round 2 — revising
  ...revision...
==============================================================
  FINAL PIECE  (2 round(s) of revision)
==============================================================
  ...
```

## Bus quickstart

```python
import asyncio
from agentbus import MessageBus, Node, ObserverNode, Topic
from agentbus.message import Message
from agentbus.schemas.common import InboundChat, OutboundChat


class EchoNode(Node):
    name = "echo"
    subscriptions = ["/inbound"]
    publications = ["/outbound"]

    def __init__(self) -> None:
        self._bus = None

    async def on_init(self, bus) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        await self._bus.publish(
            "/outbound",
            OutboundChat(text=msg.payload.text[::-1], reply_to=msg.source_node),
        )


async def main() -> None:
    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[InboundChat]("/inbound", retention=10))
    bus.register_topic(Topic[OutboundChat]("/outbound", retention=10))
    bus.register_node(EchoNode())
    bus.register_node(ObserverNode())  # logs all system events

    async def seed_messages() -> None:
        await asyncio.sleep(0.05)
        for text in ("hello", "agentbus", "echo"):
            bus.publish("/inbound", InboundChat(channel="demo", sender="user", text=text))

    asyncio.create_task(seed_messages())
    await bus.spin(until=lambda: len(bus.history("/outbound", 10)) >= 3)


asyncio.run(main())
```

## Harness quickstart

The `Harness` wraps a provider-agnostic LLM loop with session persistence and automatic context compaction. It has zero coupling to the bus layer — wire it up via a `tool_executor` callback.

```python
from agentbus.harness import Harness, Session
from agentbus.harness.providers import SystemPrompt, ToolSchema
from agentbus.harness.providers.anthropic import AnthropicProvider
from agentbus.schemas.harness import ToolCall, ToolResult

provider = AnthropicProvider(
    model="claude-haiku-4-5-20251001",
    system_prompt=SystemPrompt(static_prefix="You are a helpful assistant."),
)

async def execute_tool(call: ToolCall) -> ToolResult:
    if call.name == "search":
        return ToolResult(tool_call_id=call.id, output="results here")
    return ToolResult(tool_call_id=call.id, error="unknown tool")

harness = Harness(
    provider=provider,
    tool_executor=execute_tool,
    tools=[ToolSchema(name="search", description="Search the web")],
    session=Session(),
)

response = await harness.run("search for the latest news")
```

Sessions persist to `~/.agentbus/sessions/<session_id>/main.json`. Call `harness.run()` multiple times to continue the same conversation. Fork a session with `session.fork(from_turn_index)` to branch without mutating the parent.

## Introspection

With `socket_path` set (the default), a running bus exposes a Unix socket at `/tmp/agentbus.sock`. Use the CLI to inspect live state:

```bash
agentbus topic list
agentbus topic echo /tools/request
agentbus node list
agentbus node info planner
agentbus graph --format mermaid
agentbus channels list               # registered channel plugins
agentbus channels setup slack        # interactive setup wizard
```

Or query programmatically:

```python
bus.topics()   # list[TopicInfo]
bus.nodes()    # list[NodeInfo]
bus.graph()    # BusGraph
bus.history("/inbound", n=10)  # list[Message]
await bus.wait_for("/outbound", lambda m: m.payload.reply_to == "planner")
```

## Launch from config

```yaml
# agentbus.yaml
topics:
  - name: /inbound
    schema: myapp.schemas:InboundChat
    retention: 100
  - name: /outbound
    schema: myapp.schemas:OutboundChat

nodes:
  - class: myapp.nodes:PlannerNode
  - class: myapp.nodes:ExecutorNode
  - class: agentbus.nodes.observer:ObserverNode
```

```python
from agentbus.launch import launch_sync
launch_sync("agentbus.yaml")
```

## System topics

The bus auto-registers these topics — nodes never publish to them directly:

| Topic | Payload | When |
|-------|---------|------|
| `/system/lifecycle` | `LifecycleEvent` | Node state transitions |
| `/system/heartbeat` | `Heartbeat` | Every 30 seconds |
| `/system/backpressure` | `BackpressureEvent` | Queue overflow |
| `/system/telemetry` | `TelemetryEvent` | Harness events |
| `/system/channels` | `ChannelStatus` | Channel gateway transitions (`starting` / `connected` / `reconnecting` / `error` / `stopped`) |

Subscribe to `/system/**` with `ObserverNode` to get structured logs of everything.

## Integrations

All of the following are optional — omit the section from `agentbus.yaml` and nothing is wired up.

### MCP servers

Spawn [Model Context Protocol](https://modelcontextprotocol.io/) stdio servers and expose their advertised tools to the planner under namespaced names (`mcp__<server>__<tool>`):

```yaml
# agentbus.yaml
mcp_servers:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

Install with `uv sync --extra mcp`. The gateway coexists with the built-in tool node — each silently drops tools it doesn't own.

### Memory node

Consolidate chat turns into a local searchable store. Embeddings default to Ollama's `nomic-embed-text`, persisted to a SQLite file with a `memory_search` tool auto-registered with the planner.

```yaml
memory:
  enabled: true
  provider: ollama
  model: nomic-embed-text
  base_url: http://localhost:11434
  db_path: ~/.agentbus/memory.db
```

Pure-Python cosine similarity — no numpy, no vector-store dependency.

### Multi-channel gateways

Bridge `agentbus chat` to external chat platforms. Each gateway is a `GatewayNode` that publishes to `/inbound` and subscribes to `/outbound`; `OutboundChat.channel` and `channel_name` on the gateway keep multiple channels from stepping on each other. Every gateway also publishes `ChannelStatus` updates to `/system/channels` and is guarded by a 5-failure circuit breaker.

```yaml
channels:
  slack:
    enabled: true
    app_token: ${SLACK_APP_TOKEN}   # xapp-... (Socket Mode)
    bot_token: ${SLACK_BOT_TOKEN}   # xoxb-...
    allowed_channels: ["C01234"]
    allowed_senders: ["U01234"]
  telegram:
    enabled: true
    bot_token: ${TELEGRAM_BOT_TOKEN}
    allowed_chats: [12345]
```

Install with `uv sync --extra slack` / `--extra telegram` / `--extra channels`. Use `agentbus channels setup <name>` to run an interactive wizard that writes the section back to `agentbus.yaml`.

## Documentation

Full reference documentation lives in [`docs/`](docs/):

| Doc | Contents |
|-----|----------|
| [`docs/concepts.md`](docs/concepts.md) | Design philosophy, layer architecture, core abstractions, message lifecycle, wildcard patterns |
| [`docs/bus.md`](docs/bus.md) | `Message[T]`, `Topic[T]`, `Node`, `BusHandle`, `MessageBus` — all methods with signatures |
| [`docs/harness.md`](docs/harness.md) | `Harness`, `Session`, providers, `ToolSchema`, `Extension` hooks, compaction, testing interface |
| [`docs/schemas.md`](docs/schemas.md) | All Pydantic schemas (`harness`, `common`, `system`) and introspection dataclasses |
| [`docs/cli.md`](docs/cli.md) | CLI commands, socket protocol, programmatic API |
| [`docs/launch.md`](docs/launch.md) | YAML/JSON config reference, `launch_sync`, `build_bus_from_config` |
| [`docs/examples.md`](docs/examples.md) | Annotated example walkthroughs with key patterns |

## Development

```bash
uv sync --extra dev
uv run pytest tests/ -v
uv run pytest tests/test_harness_loop.py -v  # single file
```

`asyncio_mode = "auto"` is set globally — async test functions don't need `@pytest.mark.asyncio`.
