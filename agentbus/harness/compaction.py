from dataclasses import dataclass
from typing import Awaitable, Callable

from agentbus.schemas.harness import ContentBlock, ConversationTurn
from agentbus.utils import CircuitBreaker

AUTOCOMPACT_BUFFER_TOKENS = 13_000
MAX_SUMMARY_TOKENS = 20_000
FILE_REINJECT_CAP_TOKENS = 5_000
POST_COMPACT_BUDGET_TOKENS = 50_000
MAX_CONSECUTIVE_COMPACT_FAILURES = 3
MAX_TOOL_OUTPUT_TOKENS = 4_000
TRUNCATED_TOOL_OUTPUT_TOKENS = 64


def estimate_tokens(value: str) -> int:
    return max(1, len(value) // 4)


def turn_text(turn: ConversationTurn) -> str:
    if isinstance(turn.content, str):
        return turn.content
    parts = []
    for block in turn.content:
        if isinstance(block, ContentBlock) and block.text:
            parts.append(block.text)
    return "\n".join(parts)


@dataclass(slots=True)
class CompactResult:
    messages: list[ConversationTurn]
    compacted: bool
    summary: str | None = None


class MicroCompact:
    """Cheap, stateless tool output truncation."""

    def __init__(
        self,
        *,
        max_tool_output_tokens: int = MAX_TOOL_OUTPUT_TOKENS,
        truncated_output_tokens: int = TRUNCATED_TOOL_OUTPUT_TOKENS,
    ) -> None:
        self.max_tool_output_tokens = max_tool_output_tokens
        self.truncated_output_tokens = truncated_output_tokens

    async def compact(self, messages: list[ConversationTurn]) -> list[ConversationTurn]:
        compacted: list[ConversationTurn] = []
        for turn in messages:
            if turn.role != "tool_result":
                compacted.append(turn)
                continue
            token_count = turn.token_count or estimate_tokens(turn_text(turn))
            if token_count <= self.max_tool_output_tokens:
                compacted.append(turn)
                continue
            placeholder = (
                f"[tool output truncated: {token_count} tokens -> "
                f"{self.truncated_output_tokens} tokens]"
            )
            compacted.append(
                turn.model_copy(
                    update={
                        "content": placeholder,
                        "token_count": estimate_tokens(placeholder),
                    }
                )
            )
        return compacted


class AutoCompact:
    """LLM-backed context compaction with a circuit breaker."""

    def __init__(
        self,
        summarize: Callable[[list[ConversationTurn]], Awaitable[str]],
        *,
        recent_turns: int = 4,
    ) -> None:
        self._summarize = summarize
        self._recent_turns = recent_turns
        self.breaker = CircuitBreaker(
            "autocompact",
            max_failures=MAX_CONSECUTIVE_COMPACT_FAILURES,
        )

    async def compact(self, messages: list[ConversationTurn]) -> CompactResult:
        if self.breaker.is_open:
            return CompactResult(messages=list(messages), compacted=False)

        try:
            summary = await self._summarize(messages)
        except Exception:
            self.breaker.record_failure()
            return CompactResult(messages=list(messages), compacted=False)

        self.breaker.record_success()
        recent = list(messages[-self._recent_turns :]) if self._recent_turns > 0 else []
        summary_turn = ConversationTurn(
            role="assistant",
            content=f"[conversation summary]\n{summary[:MAX_SUMMARY_TOKENS]}",
            token_count=estimate_tokens(summary[:MAX_SUMMARY_TOKENS]),
        )
        return CompactResult(messages=[summary_turn, *recent], compacted=True, summary=summary)


class FullCompact(AutoCompact):
    """MVP full compaction: same strategy as AutoCompact over the full history."""


__all__ = [
    "AUTOCOMPACT_BUFFER_TOKENS",
    "AutoCompact",
    "CompactResult",
    "FILE_REINJECT_CAP_TOKENS",
    "FullCompact",
    "MAX_CONSECUTIVE_COMPACT_FAILURES",
    "MAX_SUMMARY_TOKENS",
    "MAX_TOOL_OUTPUT_TOKENS",
    "MicroCompact",
    "POST_COMPACT_BUDGET_TOKENS",
    "TRUNCATED_TOOL_OUTPUT_TOKENS",
    "estimate_tokens",
    "turn_text",
]
