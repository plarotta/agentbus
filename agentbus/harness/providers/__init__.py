from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentbus.schemas.harness import ToolCall


@dataclass(slots=True)
class Chunk:
    """A streamed provider delta.

    Text deltas are appended into the assistant response. Tool call fields may
    arrive incrementally; the harness accumulator stitches them together.
    """

    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: str | dict[str, Any] | None = None


@dataclass(slots=True)
class ToolSchema:
    """Provider-facing tool declaration."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SystemPrompt:
    """Structured system prompt with a cacheable/static boundary."""

    static_prefix: str
    dynamic_suffix: str = ""

    def render(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": self.static_prefix,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if self.dynamic_suffix:
            blocks.append({"type": "text", "text": self.dynamic_suffix})
        return blocks

    def render_plain(self) -> str:
        if not self.dynamic_suffix:
            return self.static_prefix
        return f"{self.static_prefix}\n{self.dynamic_suffix}"


class Provider(Protocol):
    async def complete(
        self,
        messages: list[Any],
        tools: list[ToolSchema],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        signal: Any | None = None,
    ) -> AsyncIterator[Chunk]: ...

    @property
    def context_window(self) -> int: ...

    def count_tokens(self, messages: list[Any]) -> int: ...


def chunk_to_tool_call(chunk: Chunk) -> ToolCall | None:
    """Convert a single complete chunk into a ToolCall when possible."""
    if not chunk.tool_call_id or not chunk.tool_name:
        return None
    arguments = chunk.tool_arguments if isinstance(chunk.tool_arguments, dict) else {}
    return ToolCall(id=chunk.tool_call_id, name=chunk.tool_name, arguments=arguments)


__all__ = [
    "Chunk",
    "Provider",
    "SystemPrompt",
    "ToolSchema",
    "chunk_to_tool_call",
]
