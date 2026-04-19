# AgentBus

![AgentBus](assets/banner.png)

**AgentBus is a typed, observable message bus for building and debugging multi-agent LLM systems.**

Not another agent framework. It is the infrastructure layer that makes multi-agent systems inspectable, decoupled, and composable — locally first, on a single machine, with zero cloud dependency.

---

## Why AgentBus?

Most agent systems look like this:

```text
loop:
  call tool
  get result
  repeat
```

That works until you have more than one agent, or you want to see what's happening inside, or you need to swap a tool without rewiring the planner. AgentBus replaces direct function calls with **typed message passing**:

```text
PlannerNode  ──►  /tools/request  ──►  ToolExecutorNode
                                             │
PlannerNode  ◄──  /tools/result  ◄───────────┘
```

The consequences fall out of the design:

- **Observable.** Every message is a typed envelope on a named topic. Stream them live, render the wiring graph, replay from the retention buffer.
- **Decoupled.** The planner never imports the executor — or any other node. Swap, mock, or move a node without touching its callers.
- **Composable.** Tools, gateways, memory, sub-agents — all the same primitive (a `Node`), all speaking the same protocol.
- **Event-driven.** Nodes react to messages on subscribed topics, forming a reactive graph of computation. No orchestrator polling, no driver loop in your code.

---

## Mental model

- **Nodes** = agents, tools, gateways, observers — anything that reacts to messages.
- **Topics** = typed channels (`Topic[InboundChat]`, `Topic[ToolRequest]`, …). The bus enforces the schema.
- **Messages** = frozen envelopes. `source_node` is set by the bus, never by the sender — so identity can't be faked.
- **Bus** = the router + runtime. Owns dispatch, backpressure, retention, introspection.

If you've used ROS or actor systems, this will feel familiar.

---

## Observability is a first-class feature

This is the part most agent frameworks skip. With a running bus, you can introspect everything, live:

```bash
agentbus graph --format mermaid       # render the pub/sub wiring
agentbus topic echo /tools/request    # stream tool calls as they happen
agentbus topic list                   # every topic, schema, subscriber count
agentbus node list                    # every node, state, message counts
agentbus node info planner            # detail on a single node
```

Inside a chat session, the same data is a slash command away:

```text
/trace <cid>   walk the causal chain for a correlation id
/usage         token spend by role, per session
/graph         the same mermaid diagram
```

Everything the system does is a typed message on a named topic. You don't infer behavior from logs — you watch it.

---

## Tools are just nodes

This is a small idea with big consequences. A "tool" in AgentBus is a node that subscribes to `/tools/request` and publishes to `/tools/result`:

```python
class CalculatorNode(Node):
    name = "calculator"
    subscriptions = ["/tools/request"]
    publications = ["/tools/result"]

    async def on_message(self, msg: Message) -> None:
        if msg.payload.tool != "calculate":
            return  # silently drop — another tool node will handle it
        out = str(eval(msg.payload.params["expression"], {"__builtins__": {}}))
        await self._bus.publish(
            "/tools/result",
            ToolResult(tool_call_id=msg.id, output=out),
            correlation_id=msg.correlation_id,
        )
```

Because tools are nodes, they're:

- **Replaceable** — mock one out in a test by registering a different node.
- **Distributable** — a tool node can live in another process, another host, behind a socket.
- **Observable** — every tool call is a message you can trace, echo, and replay.

The `ChatToolNode`, `MCPGatewayNode`, `MemoryNode`, and swarm `SwarmCoordinatorNode` all use this exact pattern and compose on the same topic.

---

## Bus quickstart

```python
import asyncio
from agentbus import MessageBus, Node, Topic
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
        for text in ("hello", "agentbus"):
            bus.publish("/inbound", InboundChat(channel="demo", sender="user", text=text))

    asyncio.create_task(seed())
    await bus.spin(until=lambda: len(bus.history("/outbound", 10)) >= 2)


asyncio.run(main())
```

That's the whole model: declare topics, register nodes, publish, spin. No framework-owned control flow.

---

## First run

The fastest way to a working chat session is the interactive wizard. It picks a provider + model, selects built-in tools, toggles memory, and walks each channel plugin's own sub-flow:

```bash
uv sync --extra tui
uv run agentbus setup        # writes ./agentbus.yaml atomically (.bak preserved)
uv run agentbus chat         # interactive chat TUI with live tool-call streaming
```

`setup` and `chat` share a single visual identity — block-art banner, cyan accent, muted `·` separators. See [`docs/cli.md`](docs/cli.md#setup) for flags and exit codes.

---

## Local-first, distributed-ready

AgentBus is local-first by default — a single `asyncio` event loop, zero cloud dependency, zero external services. But the topic abstraction is the same whether two nodes live in the same process or different ones: the optional Unix-socket introspection server at `/tmp/agentbus.sock` is the seam for multi-process wiring, remote tool execution, and cross-host observation. Distributed routing is on the roadmap, not bolted on — the message envelope already carries everything it needs.

---

## Launch from config

For anything beyond a single script, declare topology in YAML:

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

Or launch from the CLI:

```bash
agentbus launch agentbus.yaml
agentbus daemon start agentbus.yaml       # pidfile-locked foreground run
```

---

## System topics

The bus auto-registers these — nodes never publish to them directly. Subscribe to `/system/**` with `ObserverNode` to get structured logs of everything.

| Topic                  | Payload             | When                                                                                          |
| ---------------------- | ------------------- | --------------------------------------------------------------------------------------------- |
| `/system/lifecycle`    | `LifecycleEvent`    | Node state transitions                                                                        |
| `/system/heartbeat`    | `Heartbeat`         | Every 30 seconds                                                                              |
| `/system/backpressure` | `BackpressureEvent` | Queue overflow                                                                                |
| `/system/telemetry`    | `TelemetryEvent`    | Harness events                                                                                |
| `/system/channels`     | `ChannelStatus`     | Channel gateway transitions (`starting` / `connected` / `reconnecting` / `error` / `stopped`) |

---

## Integrations

All optional — omit the section from `agentbus.yaml` and nothing is wired up. Each integration is a node (or a group of nodes) that subscribes to the same topics as everything else.

- **MCP servers** — spawn [Model Context Protocol](https://modelcontextprotocol.io/) stdio servers and expose their tools under `mcp__<server>__<tool>`.
- **Memory** — embed each chat turn into a local SQLite vector store and auto-register a `memory_search` tool. Pure-Python cosine similarity; no numpy.
- **Channel gateways** — Slack (Socket Mode) and Telegram (long-poll) bridge external messages to `/inbound` / `/outbound`. Allowlists, self-echo filters, reconnect with backoff, and circuit breakers are built in.
- **Swarm (hub-and-spoke)** — a coordinator LLM exposes a `dispatch_subagent` tool; each sub-agent lives on `/swarm/<name>/inbound` + `/swarm/<name>/outbound` and runs a fresh `Harness` per dispatch.

Config shapes live in [`docs/launch.md`](docs/launch.md); reliability details in [`docs/concepts.md`](docs/concepts.md).

---

## Install

With [uv](https://github.com/astral-sh/uv) (recommended):

```bash
uv sync --extra anthropic       # one provider
uv sync --extra all             # everything: tui, mcp, channels, all providers
```

Or with pip:

```bash
pip install "agentbus[anthropic]"
pip install "agentbus[all]"
```

Full extras list: `anthropic`, `openai`, `ollama`, `cli`, `tui`, `mcp`, `slack`, `telegram`, `channels`, `all`.

---

## Documentation

| Doc                                    | Contents                                                                                        |
| -------------------------------------- | ----------------------------------------------------------------------------------------------- |
| [`docs/concepts.md`](docs/concepts.md) | Design philosophy, layer architecture, core abstractions, message lifecycle, wildcard patterns  |
| [`docs/bus.md`](docs/bus.md)           | `Message[T]`, `Topic[T]`, `Node`, `BusHandle`, `MessageBus` — all methods with signatures       |
| [`docs/harness.md`](docs/harness.md)   | `Harness`, `Session`, providers, `ToolSchema`, `Extension` hooks, compaction, testing interface |
| [`docs/schemas.md`](docs/schemas.md)   | All Pydantic schemas (`harness`, `common`, `system`) and introspection dataclasses              |
| [`docs/cli.md`](docs/cli.md)           | CLI commands, socket protocol, programmatic API                                                 |
| [`docs/launch.md`](docs/launch.md)     | YAML/JSON config reference, `launch_sync`, `build_bus_from_config`                              |
| [`docs/examples.md`](docs/examples.md) | Annotated walkthroughs of every runnable example in `examples/`                                 |

---

## Development

```bash
uv sync --extra dev
uv run pytest tests/ -v
uv run pytest tests/test_harness_loop.py -v  # single file
```

`asyncio_mode = "auto"` is set globally — async test functions don't need `@pytest.mark.asyncio`.
