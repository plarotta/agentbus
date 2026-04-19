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

With [uv](https://github.com/astral-sh/uv) (recommended):

```bash
uv sync --extra anthropic     # or: openai, ollama, cli, tui, mcp, slack, telegram, channels, all
```

Plain pip works too — `pip install "agentbus[anthropic]"`, etc.

## First run

For the fastest path to a working config, run the interactive wizard — it
picks provider + model, tools, memory, and each channel plugin's own
sub-flow, then writes `agentbus.yaml` atomically and runs `agentbus
doctor` against the new file:

```bash
uv sync --extra tui
uv run agentbus setup        # writes ./agentbus.yaml (.bak preserved)
uv run agentbus chat         # launches the chat TUI (same theme)
```

See [`cli.md`](cli.md#setup) for flags and exit codes.

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

    async def seed() -> None:
        await asyncio.sleep(0.05)
        for text in ("hello", "agentbus", "echo"):
            bus.publish("/inbound", InboundChat(channel="demo", sender="user", text=text))

    asyncio.create_task(seed())
    await bus.spin(until=lambda: len(bus.history("/outbound", 10)) >= 3)


asyncio.run(main())
```

## Harness quickstart

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

## Introspection

With a running bus, inspect live state from the CLI:

```bash
agentbus topic list
agentbus topic echo /tools/request
agentbus node list
agentbus graph --format mermaid
agentbus channels list               # registered channel plugins
agentbus setup                        # themed full-config wizard
agentbus channels setup slack         # per-channel reconfigure
```

## Integrations

Optional, enabled by YAML config or by wiring nodes directly. See
[`launch.md`](launch.md) for config shapes.

- **MCP servers** — any MCP stdio server can expose its tools to the planner
  under `mcp__<server>__<tool>` names.
- **Memory node** — embeds each chat turn to a local SQLite store and
  registers a `memory_search` tool for retrieval.
- **Multi-channel gateways** — Slack (Socket Mode) and Telegram (long-poll)
  ship in-tree; both bridge external messages to `/inbound` / `/outbound`.
- **Swarm (hub-and-spoke)** — a coordinator LLM delegates to named
  sub-agents via a `dispatch_subagent` tool; each sub-agent lives on
  `/swarm/<name>/inbound` + `/swarm/<name>/outbound` and runs a fresh
  `Harness` per dispatch.

## System topics

The bus auto-registers these topics — nodes never publish to them directly:

| Topic | Payload | When |
|-------|---------|------|
| `/system/lifecycle` | `LifecycleEvent` | Node state transitions |
| `/system/heartbeat` | `Heartbeat` | Every 30 seconds |
| `/system/backpressure` | `BackpressureEvent` | Queue overflow |
| `/system/telemetry` | `TelemetryEvent` | Harness events |
| `/system/channels` | `ChannelStatus` | Multi-channel gateway lifecycle transitions |
