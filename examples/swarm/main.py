"""
Swarm — hub-and-spoke multi-agent orchestration.

A coordinator LLM decides which sub-agent to invoke via a ``dispatch_subagent``
tool. Each sub-agent lives on its own ``/swarm/<name>/inbound`` + ``/swarm/<name>/outbound``
topic pair and runs a fresh :class:`Harness` per dispatch — fresh context,
no shared state. Sub-agents never talk to each other; handoffs go through
the coordinator.

Architecture::

    /inbound ──► CoordinatorNode ──► /tools/request ──► SwarmCoordinator
                       ▲                                       │
                       │                                       ▼
                       │                       /swarm/researcher/inbound
                       │                                       │
                       │                                       ▼
                       │                          SwarmAgentNode(researcher)
                       │                                       │
                       │                                       ▼
                       │                       /swarm/researcher/outbound
                       │                                       │
                       │           (same for /swarm/writer/…)  │
                       │                                       │
                       └────────── /tools/result  ◄────────────┘
                       │
                       ▼
                   /outbound

Setup::

    uv sync --extra anthropic

Usage::

    ANTHROPIC_API_KEY=sk-ant-… uv run python examples/swarm/main.py

    # optional
    TASK="describe the harness layer" uv run python examples/swarm/main.py
"""

from __future__ import annotations

import asyncio
import os

from agentbus import MessageBus, Topic
from agentbus.chat._config import ChatConfig
from agentbus.chat._planner import ChatPlannerNode
from agentbus.chat._tools import ChatToolNode
from agentbus.harness.providers import SystemPrompt
from agentbus.harness.providers.anthropic import AnthropicProvider
from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest
from agentbus.schemas.common import ToolResult as BusToolResult
from agentbus.schemas.harness import PlannerStatus
from agentbus.swarm import SubAgentSpec, register_swarm

MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")
TASK = os.environ.get(
    "TASK",
    "Read agentbus/__init__.py, list the public exports, then write a one-sentence "
    "summary of what the package offers. Use the researcher first, then the writer.",
)


class _Capture(Node):
    """Bridges /outbound back to the driver."""

    name = "capture"
    subscriptions = ["/outbound"]
    publications: list[str] = []

    def __init__(self, q: asyncio.Queue[OutboundChat]):
        self._q = q

    async def on_message(self, msg: Message) -> None:
        await self._q.put(msg.payload)


class _StatusPrinter(Node):
    """Prints every ↳ tool dispatch so the handoffs are visible."""

    name = "status-printer"
    subscriptions = ["/planning/status"]
    publications: list[str] = []

    async def on_message(self, msg: Message) -> None:
        status: PlannerStatus = msg.payload
        if status.event == "tool_dispatched" and status.tool_name:
            label = status.tool_name
            if label == "dispatch_subagent":
                label = "dispatch_subagent"
            print(f"  ↳ {label}")


async def main() -> None:
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise SystemExit("Set ANTHROPIC_API_KEY before running this example.")

    bus = MessageBus(socket_path=None)

    bus.register_topic(Topic[InboundChat]("/inbound", retention=20))
    bus.register_topic(Topic[OutboundChat]("/outbound", retention=20))
    bus.register_topic(Topic[ToolRequest]("/tools/request", retention=20))
    bus.register_topic(Topic[BusToolResult]("/tools/result", retention=20))
    bus.register_topic(Topic[PlannerStatus]("/planning/status", retention=20))

    swarm_specs = [
        SubAgentSpec(
            name="researcher",
            description=(
                "Reads files and runs shell commands to gather information. "
                "Use for investigating the filesystem or code."
            ),
            system_prompt=(
                "You are a meticulous researcher. Use the tools to gather "
                "concrete facts and return a structured list of findings. "
                "Be concise — no prose, just the facts."
            ),
            tools=["bash", "file_read"],
        ),
        SubAgentSpec(
            name="writer",
            description=(
                "Synthesizes prose from provided research. Use for summaries "
                "and explanations. Does not have tool access."
            ),
            system_prompt=(
                "You are a clear, concise technical writer. Given a set of "
                "facts, produce polished prose — one or two sentences unless "
                "asked for more. Do not invent facts beyond what you are given."
            ),
            tools=[],
        ),
    ]
    dispatch_schema = register_swarm(
        bus,
        swarm_specs,
        ChatConfig(provider="anthropic", model=MODEL, tools=["bash", "file_read"]),
    )

    coordinator = ChatPlannerNode(
        ChatConfig(provider="anthropic", model=MODEL, tools=[]),
        provider=AnthropicProvider(
            model=MODEL,
            system_prompt=SystemPrompt(
                static_prefix=(
                    "You are a coordinator that delegates work to specialized "
                    "sub-agents via the dispatch_subagent tool. Plan which "
                    "sub-agents to call (and in what order) before dispatching. "
                    "Wait for each reply before deciding the next step. "
                    "Return the final answer as prose."
                )
            ),
        ),
        extra_tools=[dispatch_schema],
    )
    bus.register_node(coordinator)
    bus.register_node(ChatToolNode(enabled_tools=["bash", "file_read"]))

    response_q: asyncio.Queue[OutboundChat] = asyncio.Queue()
    bus.register_node(_Capture(response_q))
    bus.register_node(_StatusPrinter())

    async def send():
        await asyncio.sleep(0.05)
        bus.publish("/inbound", InboundChat(channel="demo", sender="user", text=TASK))

    asyncio.create_task(send())

    print("=" * 66)
    print("  SWARM DEMO")
    print("=" * 66)
    print(f"Task: {TASK}\n")
    print("Handoffs:")

    await bus.spin(until=lambda: not response_q.empty(), timeout=120.0)

    reply = response_q.get_nowait()
    print("\n" + "=" * 66)
    print("  FINAL ANSWER")
    print("=" * 66)
    print(reply.text)


if __name__ == "__main__":
    asyncio.run(main())
