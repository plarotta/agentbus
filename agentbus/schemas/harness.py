from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ToolCall(BaseModel):
    """A tool invocation produced by the LLM during the agent loop."""

    id: str
    name: str
    arguments: dict = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Result returned to the harness after tool execution.

    This is the harness-internal type returned by the tool_executor callback.
    The bus-facing equivalent is agentbus.schemas.common.ToolResult — they share
    the same fields so the PlannerNode bridge can map between them directly.
    """

    tool_call_id: str
    output: str | None = None
    error: str | None = None


class ContentBlock(BaseModel):
    """A single block within a multi-part message content list."""

    type: str
    text: str | None = None
    data: Any | None = None  # for image/binary blocks


class PlannerStatus(BaseModel):
    """Published to /planning/status by a PlannerNode to expose harness state.

    Enables `agentbus topic echo /planning/status` to show LLM state transitions
    in real time without the harness knowing it's being observed.
    """

    event: Literal[
        "thinking",        # LLM call started
        "tool_dispatched", # tool call sent to bus
        "tool_received",   # tool result received
        "compacting",      # context window compaction triggered
        "responding",      # final response being generated
        "error",           # harness error
    ]
    iteration: int
    context_tokens: int
    context_capacity: float  # percentage of context window used (0.0–1.0)
    tool_name: str | None = None
    detail: str | None = None


class ConversationTurn(BaseModel):
    """A single turn in the session conversation history."""

    role: Literal["user", "assistant", "tool_result"]
    content: str | list[ContentBlock]
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None  # set on tool_result turns; links result to its ToolCall
    token_count: int = 0
    timestamp: datetime = Field(default_factory=_utcnow)
