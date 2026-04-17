"""
Writer + Critic — two LLM agents collaborating via the bus.

Architecture:

    /tasks ──► WriterNode ──► /drafts ──► CriticNode ──► /feedback
                   ▲                           │
                   └───────────────────────────┘  (revision loop)
                                               │
                                        /final (last round)
                                               │
                                          OutputNode

Each agent has its own Harness and Session. The bus routes messages between
them — WriterNode never imports or calls CriticNode directly.

After ROUNDS write/critique cycles, CriticNode publishes to /final and the
bus exits. Observable in real-time with:
    agentbus topic echo /drafts
    agentbus topic echo /feedback

Setup:
    uv sync --extra anthropic

Usage:
    ANTHROPIC_API_KEY=sk-ant-... uv run python examples/writer_critic/main.py

Optional env vars:
    MODEL   — Anthropic model ID (default: claude-haiku-4-5-20251001)
    ROUNDS  — write/critique cycles (default: 2)
    TOPIC   — what to write about (default: built-in)
"""

import asyncio
import os

from pydantic import BaseModel

from agentbus import MessageBus, Node, Topic
from agentbus.harness import Harness, Session
from agentbus.harness.providers import SystemPrompt
from agentbus.harness.providers.anthropic import AnthropicProvider
from agentbus.message import Message
from agentbus.schemas.harness import ToolCall, ToolResult

MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")
ROUNDS = int(os.environ.get("ROUNDS", "2"))
TOPIC = os.environ.get(
    "TOPIC",
    "why it's worth learning to type properly",
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class WritingTask(BaseModel):
    topic: str
    max_rounds: int


class Draft(BaseModel):
    content: str
    round: int
    max_rounds: int


class Critique(BaseModel):
    feedback: str
    round: int
    is_final: bool


class FinalPiece(BaseModel):
    content: str
    rounds_completed: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _no_tools(call: ToolCall) -> ToolResult:
    return ToolResult(tool_call_id=call.id, error="no tools available")


def _make_provider(system: str) -> AnthropicProvider:
    return AnthropicProvider(
        model=MODEL,
        system_prompt=SystemPrompt(static_prefix=system),
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


class WriterNode(Node):
    """Writes a first draft, then revises based on critic feedback."""

    name = "writer"
    subscriptions = ["/tasks", "/feedback"]
    publications = ["/drafts"]
    concurrency_mode = "serial"

    def __init__(self) -> None:
        self._bus = None
        self._harness: Harness | None = None

    async def on_init(self, bus) -> None:
        self._bus = bus
        self._harness = Harness(
            provider=_make_provider(
                "You are a creative writer. Produce clear, engaging prose. "
                "When revising, address the feedback directly and improve the piece. "
                "Respond with the piece only — no preamble or commentary."
            ),
            tool_executor=_no_tools,
            tools=[],
            session=Session(),
        )

    async def on_message(self, msg: Message) -> None:
        if isinstance(msg.payload, WritingTask):
            task: WritingTask = msg.payload
            _section("Writer", f"Round 1 — writing about: {task.topic}")
            text = await self._harness.run(
                f"Write a short paragraph (4-6 sentences) about: {task.topic}"
            )
            _print_content("Writer", text)
            await self._bus.publish(
                "/drafts",
                Draft(content=text, round=1, max_rounds=task.max_rounds),
            )

        elif isinstance(msg.payload, Critique):
            critique: Critique = msg.payload
            if critique.is_final:
                return
            rev = critique.round + 1
            _section("Writer", f"Round {rev} — revising")
            text = await self._harness.run(
                f"Revise your piece based on this feedback:\n\n{critique.feedback}"
            )
            _print_content("Writer", text)
            await self._bus.publish(
                "/drafts",
                Draft(content=text, round=rev, max_rounds=critique.round + 1),
            )


class CriticNode(Node):
    """Reviews each draft and provides actionable feedback."""

    name = "critic"
    subscriptions = ["/drafts"]
    publications = ["/feedback", "/final"]
    concurrency_mode = "serial"

    def __init__(self) -> None:
        self._bus = None
        self._harness: Harness | None = None

    async def on_init(self, bus) -> None:
        self._bus = bus
        self._harness = Harness(
            provider=_make_provider(
                "You are a sharp, constructive editor. "
                "Give specific, actionable feedback in 2-3 sentences. "
                "Focus on clarity, word choice, and impact. Be direct."
            ),
            tool_executor=_no_tools,
            tools=[],
            session=Session(),
        )

    async def on_message(self, msg: Message) -> None:
        draft: Draft = msg.payload
        is_final = draft.round >= draft.max_rounds

        _section("Critic", f"Reviewing round {draft.round} draft")
        feedback = await self._harness.run(
            f"Review this piece and give feedback:\n\n{draft.content}"
        )
        _print_content("Critic", feedback)

        await self._bus.publish(
            "/feedback",
            Critique(feedback=feedback, round=draft.round, is_final=is_final),
        )

        if is_final:
            await self._bus.publish(
                "/final",
                FinalPiece(content=draft.content, rounds_completed=draft.round),
            )


class OutputNode(Node):
    """Displays the final polished piece."""

    name = "output"
    subscriptions = ["/final"]
    publications = []

    async def on_message(self, msg: Message) -> None:
        piece: FinalPiece = msg.payload
        print(f"\n{'=' * 60}")
        print(f"  FINAL PIECE  ({piece.rounds_completed} round(s) of revision)")
        print(f"{'=' * 60}")
        print(piece.content)
        print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _section(agent: str, label: str) -> None:
    print(f"\n[{agent}] {label}")
    print("-" * 50)


def _print_content(agent: str, text: str) -> None:
    for line in text.strip().splitlines():
        print(f"  {line}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    print(f"Model: {MODEL}  |  Rounds: {ROUNDS}  |  Topic: {TOPIC}\n")

    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[WritingTask]("/tasks", retention=1))
    bus.register_topic(Topic[Draft]("/drafts", retention=10))
    bus.register_topic(Topic[Critique]("/feedback", retention=10))
    bus.register_topic(Topic[FinalPiece]("/final", retention=1))

    bus.register_node(WriterNode())
    bus.register_node(CriticNode())
    bus.register_node(OutputNode())

    async def kick_off() -> None:
        await asyncio.sleep(0.1)
        bus.publish("/tasks", WritingTask(topic=TOPIC, max_rounds=ROUNDS))

    asyncio.create_task(kick_off())

    await bus.spin(
        until=lambda: len(bus.history("/final", 2)) >= 1,
        timeout=180.0,
    )

    # Show session stats for both agents
    for name, handle in bus._nodes.items():
        node = handle.node
        if hasattr(node, "_harness") and node._harness is not None:
            h = node._harness
            print(f"  {name:10s}  turns={len(h.session.turns)}  tokens={h.session.total_tokens()}")


if __name__ == "__main__":
    asyncio.run(main())
