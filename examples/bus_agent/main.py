"""
Full bus integration example — Harness wired into the pub/sub system.

Architecture:

    user input
        │
        ▼
    /inbound  ──►  PlannerNode  ──► /tools/request ──►  ToolExecutorNode
                       │                                        │
                       │               /tools/result ◄─────────┘
                       │
                       ▼
                   /outbound  ──►  OutputNode (prints responses)

The LLM loop (Harness) lives entirely inside PlannerNode. Tool calls are
routed through the bus — the harness never calls ToolExecutorNode directly.
Everything is observable via /system/** topics (ObserverNode).

Setup:
    uv sync --extra anthropic

Usage:
    ANTHROPIC_API_KEY=sk-ant-... uv run python examples/bus_agent/main.py

Optional env vars:
    MODEL — Anthropic model ID (default: claude-haiku-4-5-20251001)
"""

import asyncio
import math
import os
from datetime import UTC, datetime

from agentbus import MessageBus, Node, ObserverNode, Topic
from agentbus.harness import Harness, Session
from agentbus.harness.providers import SystemPrompt, ToolSchema
from agentbus.harness.providers.anthropic import AnthropicProvider
from agentbus.message import Message
from agentbus.schemas.common import InboundChat, OutboundChat
from agentbus.schemas.common import ToolRequest as BusToolRequest
from agentbus.schemas.common import ToolResult as BusToolResult
from agentbus.schemas.harness import ToolCall
from agentbus.schemas.harness import ToolResult as HarnessToolResult

# ---------------------------------------------------------------------------
# Tools exposed to the LLM
# ---------------------------------------------------------------------------

TOOLS = [
    ToolSchema(
        name="calculate",
        description="Evaluate a mathematical expression using the Python math module.",
        input_schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "e.g. '2 ** 10' or 'math.sqrt(2)'"}
            },
            "required": ["expression"],
        },
    ),
    ToolSchema(
        name="get_time",
        description="Return the current UTC date and time.",
        input_schema={"type": "object", "properties": {}},
    ),
]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


class PlannerNode(Node):
    """Receives /inbound messages, runs an LLM loop, routes tool calls via the bus."""

    name = "planner"
    subscriptions = ["/inbound"]
    publications = ["/outbound", "/tools/request"]
    concurrency_mode = "serial"  # LLM loop is stateful — process one message at a time

    def __init__(self) -> None:
        self._bus = None
        self._harness: Harness | None = None

    async def on_init(self, bus) -> None:
        self._bus = bus

        async def tool_executor(call: ToolCall) -> HarnessToolResult:
            """Route tool call through the bus; await the result."""
            reply: Message = await self._bus.request(
                "/tools/request",
                BusToolRequest(tool=call.name, params=call.arguments),
                reply_on="/tools/result",
                timeout=30.0,
            )
            bus_result: BusToolResult = reply.payload
            return HarnessToolResult(
                tool_call_id=call.id,
                output=bus_result.output,
                error=bus_result.error,
            )

        self._harness = Harness(
            provider=AnthropicProvider(
                model=os.environ.get("MODEL", "claude-haiku-4-5-20251001"),
                system_prompt=SystemPrompt(
                    static_prefix="You are a helpful assistant. Use tools when you need precise answers."
                ),
            ),
            tool_executor=tool_executor,
            tools=TOOLS,
            session=Session(),
        )

    async def on_message(self, msg: Message) -> None:
        inbound: InboundChat = msg.payload
        print(f"\nUser [{inbound.sender}]: {inbound.text}")
        response = await self._harness.run(inbound.text)
        await self._bus.publish(
            "/outbound",
            OutboundChat(text=response, reply_to=inbound.sender),
        )


class ToolExecutorNode(Node):
    """Executes tool calls received from /tools/request and publishes results."""

    name = "tool_executor"
    subscriptions = ["/tools/request"]
    publications = ["/tools/result"]

    def __init__(self) -> None:
        self._bus = None

    async def on_init(self, bus) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        request: BusToolRequest = msg.payload
        output = await _run_tool(request.tool, request.params)
        await self._bus.publish(
            "/tools/result",
            BusToolResult(tool_call_id=msg.id, output=output),
            correlation_id=msg.correlation_id,  # echo back so bus.request() resolves
        )


class OutputNode(Node):
    """Prints responses as they arrive on /outbound."""

    name = "output"
    subscriptions = ["/outbound"]
    publications = []

    async def on_message(self, msg: Message) -> None:
        response: OutboundChat = msg.payload
        print(f"Assistant: {response.text}")


# ---------------------------------------------------------------------------
# Tool implementations (pure functions — no bus coupling)
# ---------------------------------------------------------------------------


async def _run_tool(name: str, params: dict) -> str:
    match name:
        case "calculate":
            expr = params.get("expression", "")
            result = eval(expr, {"__builtins__": {}}, {"math": math})
            return str(result)
        case "get_time":
            return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        case _:
            return f"unknown tool: {name}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    bus = MessageBus()  # default socket at /tmp/agentbus.sock
    bus.register_topic(Topic[InboundChat]("/inbound", retention=20))
    bus.register_topic(Topic[OutboundChat]("/outbound", retention=20))
    bus.register_topic(Topic[BusToolRequest]("/tools/request", retention=10))
    bus.register_topic(Topic[BusToolResult]("/tools/result", retention=10))

    bus.register_node(PlannerNode())
    bus.register_node(ToolExecutorNode())
    bus.register_node(OutputNode())
    bus.register_node(ObserverNode())

    questions = [
        "What is 2 raised to the power of 32?",
        "What is the square root of that result?",
        "What time is it right now?",
    ]

    async def seed_messages() -> None:
        await asyncio.sleep(0.1)  # let nodes initialize
        for text in questions:
            bus.publish("/inbound", InboundChat(channel="demo", sender="user", text=text))
            # Wait for the response before sending the next question so output stays ordered.
            await bus.wait_for("/outbound", lambda m: True, timeout=120.0)

    asyncio.create_task(seed_messages())

    # Stop once every question has a response on /outbound.
    # The warnings "no publishers / no subscribers" for /inbound and /tools/result
    # are expected: /inbound is seeded externally; /tools/result uses request/reply
    # correlation (bus._pending_requests), not a declared node subscription.
    await bus.spin(
        until=lambda: len(bus.history("/outbound", len(questions) + 1)) >= len(questions),
        timeout=120.0,
    )


if __name__ == "__main__":
    asyncio.run(main())
