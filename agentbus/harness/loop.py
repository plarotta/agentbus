import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from agentbus.harness.compaction import (
    AUTOCOMPACT_BUFFER_TOKENS,
    AutoCompact,
    CompactResult,
    MicroCompact,
    estimate_tokens,
    turn_text,
)
from agentbus.harness.extensions import (
    Extension,
    run_on_before_compact,
    run_on_before_llm,
    run_on_context,
    run_on_error,
    run_on_response,
    run_on_tool_call,
    run_on_tool_result,
)
from agentbus.harness.providers import Chunk, Provider, ToolSchema
from agentbus.harness.session import Session
from agentbus.schemas.harness import ConversationTurn, ToolCall, ToolResult


class HarnessDeps(Protocol):
    def call_provider(
        self,
        messages: list[ConversationTurn],
        tools: list[ToolSchema],
        **kwargs: Any,
    ) -> AsyncIterator[Chunk] | Awaitable[AsyncIterator[Chunk]]: ...

    def microcompact(
        self, messages: list[ConversationTurn]
    ) -> list[ConversationTurn] | Awaitable[list[ConversationTurn]]: ...

    def autocompact(
        self, messages: list[ConversationTurn]
    ) -> CompactResult | Awaitable[CompactResult]: ...

    def uuid(self) -> str: ...


@dataclass(slots=True)
class ChunkAccumulator:
    text_parts: list[str] = field(default_factory=list)
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, chunk: Chunk) -> None:
        if chunk.text:
            self.text_parts.append(chunk.text)

        if chunk.tool_call_id:
            data = self.tool_calls.setdefault(
                chunk.tool_call_id,
                {"name": "", "arguments": ""},
            )
            if chunk.tool_name:
                data["name"] = chunk.tool_name
            if isinstance(chunk.tool_arguments, dict):
                data["arguments"] = chunk.tool_arguments
            elif isinstance(chunk.tool_arguments, str):
                current = data.get("arguments", "")
                if isinstance(current, dict):
                    data["arguments"] = chunk.tool_arguments
                else:
                    data["arguments"] = f"{current}{chunk.tool_arguments}"

    def build_tool_calls(self) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for tool_call_id, data in self.tool_calls.items():
            arguments = data.get("arguments", {})
            if isinstance(arguments, str):
                stripped = arguments.strip()
                if not stripped:
                    arguments = {}
                else:
                    try:
                        arguments = json.loads(stripped)
                    except json.JSONDecodeError:
                        arguments = {"raw": stripped}
            tool_calls.append(
                ToolCall(
                    id=tool_call_id,
                    name=data.get("name", ""),
                    arguments=arguments,
                )
            )
        return tool_calls

    def build_turn(self) -> ConversationTurn:
        content = "".join(self.text_parts)
        tool_calls = self.build_tool_calls()
        return ConversationTurn(
            role="assistant",
            content=content,
            tool_calls=tool_calls or None,
            token_count=estimate_tokens(content) if content else 0,
        )


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _turns_token_count(turns: Sequence[ConversationTurn]) -> int:
    total = 0
    for turn in turns:
        total += turn.token_count or estimate_tokens(turn_text(turn))
    return total


def _as_provider_messages(turns: Sequence[ConversationTurn]) -> list[dict[str, Any]]:
    messages = []
    for turn in turns:
        entry: dict[str, Any] = {
            "role": turn.role,
            "content": turn.model_dump(mode="json")["content"],
        }
        if turn.tool_calls:
            entry["tool_calls"] = [
                tool_call.model_dump(mode="json") for tool_call in turn.tool_calls
            ]
        if turn.tool_call_id is not None:
            entry["tool_call_id"] = turn.tool_call_id
        messages.append(entry)
    return messages


async def _call_tool_executor(
    tool_executor: Callable[[ToolCall], Awaitable[ToolResult] | ToolResult],
    tool_call: ToolCall,
) -> ToolResult:
    result = tool_executor(tool_call)
    resolved = await _maybe_await(result)
    if isinstance(resolved, ToolResult):
        return resolved
    return ToolResult.model_validate(resolved)


class ProductionDeps:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self._microcompact = MicroCompact()
        self._autocompact = AutoCompact(self._summarize)

    def call_provider(
        self,
        messages: list[ConversationTurn],
        tools: list[ToolSchema],
        **kwargs: Any,
    ) -> AsyncIterator[Chunk]:
        return self.provider.complete(_as_provider_messages(messages), tools, **kwargs)

    async def microcompact(self, messages: list[ConversationTurn]) -> list[ConversationTurn]:
        return await self._microcompact.compact(messages)

    async def autocompact(self, messages: list[ConversationTurn]) -> CompactResult:
        return await self._autocompact.compact(messages)

    def uuid(self) -> str:
        return str(uuid4())

    async def _summarize(self, messages: list[ConversationTurn]) -> str:
        stream = self.provider.complete(
            _as_provider_messages(messages),
            [],
            temperature=0.0,
            max_tokens=512,
        )
        parts: list[str] = []
        async for chunk in stream:
            if chunk.text:
                parts.append(chunk.text)
        return "".join(parts).strip() or "Conversation compacted."


def production_deps(provider: Provider) -> ProductionDeps:
    return ProductionDeps(provider)


class Harness:
    """Standalone agent loop connected to the outside world via tool_executor."""

    def __init__(
        self,
        *,
        provider: Provider | None = None,
        tool_executor: Callable[[ToolCall], Awaitable[ToolResult] | ToolResult],
        tools: list[ToolSchema] | None = None,
        session: Session | None = None,
        extensions: Sequence[Extension] | None = None,
        deps: HarnessDeps | None = None,
        max_iterations: int = 25,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> None:
        if deps is None and provider is None:
            raise ValueError("Harness requires either a provider or explicit deps")
        self.provider = provider
        self.tool_executor = tool_executor
        self.tools = list(tools or [])
        self.session = session or Session()
        self.extensions = list(extensions or [])
        self.deps = deps or production_deps(provider)  # type: ignore[arg-type]
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.stop = stop

    @property
    def context_window(self) -> int:
        if self.provider is None:
            return 128_000
        return self.provider.context_window

    async def run(self, user_input: str) -> str:
        try:
            self.session.append(
                ConversationTurn(
                    role="user",
                    content=user_input,
                    token_count=estimate_tokens(user_input),
                )
            )
            return await self._run_loop()
        except Exception as exc:
            fallback = run_on_error(exc, self.extensions)
            if fallback is None:
                raise
            final = run_on_response(fallback, self.extensions)
            self.session.append(
                ConversationTurn(
                    role="assistant",
                    content=final,
                    token_count=estimate_tokens(final),
                )
            )
            self.session.save()
            return final

    async def _run_loop(self) -> str:
        iteration = 0

        while iteration < self.max_iterations:
            iteration += 1
            messages = run_on_context(list(self.session.turns), self.extensions)
            messages = await self._maybe_compact(messages)
            llm_messages, llm_tools = run_on_before_llm(messages, list(self.tools), self.extensions)
            response_turn = await self._provider_turn(llm_messages, llm_tools)

            if response_turn.tool_calls:
                # Append the assistant turn with tool_calls before processing results.
                # Providers (Anthropic, OpenAI) require tool_calls in history before
                # the corresponding tool_result turns.
                self.session.append(response_turn)
                executed_any = False
                for tool_call in response_turn.tool_calls:
                    prepared_call = run_on_tool_call(tool_call, self.extensions)
                    if prepared_call is None:
                        continue
                    executed_any = True
                    tool_result = await _call_tool_executor(self.tool_executor, prepared_call)
                    tool_result = run_on_tool_result(prepared_call, tool_result, self.extensions)
                    tool_output = tool_result.output or tool_result.error or ""
                    self.session.append(
                        ConversationTurn(
                            role="tool_result",
                            content=tool_output,
                            tool_call_id=prepared_call.id,
                            token_count=estimate_tokens(tool_output),
                        )
                    )
                    self.session.turns = await _maybe_await(
                        self.deps.microcompact(list(self.session.turns))
                    )
                if executed_any:
                    continue

            response_text = run_on_response(str(response_turn.content), self.extensions)
            self.session.append(
                response_turn.model_copy(
                    update={
                        "content": response_text,
                        "token_count": estimate_tokens(response_text),
                        "tool_calls": None,
                    }
                )
            )
            self.session.save()
            return response_text

        forced_messages = run_on_context(list(self.session.turns), self.extensions)
        forced_messages = await self._maybe_compact(forced_messages)
        forced_turn = await self._provider_turn(forced_messages, [])
        final_text = run_on_response(str(forced_turn.content), self.extensions)
        self.session.append(
            forced_turn.model_copy(
                update={
                    "content": final_text,
                    "token_count": estimate_tokens(final_text),
                    "tool_calls": None,
                }
            )
        )
        self.session.save()
        return final_text

    async def _maybe_compact(self, messages: list[ConversationTurn]) -> list[ConversationTurn]:
        messages = run_on_before_compact(messages, self.extensions)
        if _turns_token_count(messages) <= self.context_window - AUTOCOMPACT_BUFFER_TOKENS:
            return messages
        result = await _maybe_await(self.deps.autocompact(messages))
        if result.compacted:
            self.session.turns = list(result.messages)
            return list(result.messages)
        return messages

    async def _provider_turn(
        self,
        messages: list[ConversationTurn],
        tools: list[ToolSchema],
    ) -> ConversationTurn:
        stream = await _maybe_await(
            self.deps.call_provider(
                messages,
                tools,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stop=self.stop,
                signal=None,
            )
        )
        accumulator = ChunkAccumulator()
        async for chunk in stream:
            accumulator.add(chunk)
        return accumulator.build_turn()


__all__ = [
    "ChunkAccumulator",
    "Harness",
    "HarnessDeps",
    "ProductionDeps",
    "production_deps",
]
