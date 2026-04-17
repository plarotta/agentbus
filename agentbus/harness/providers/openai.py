import json
from collections.abc import AsyncIterator
from typing import Any

from agentbus.harness.providers import Chunk, ToolSchema


class OpenAIProvider:
    """OpenAI-compatible provider with optional custom base URL support."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        context_window: int = 128_000,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self._context_window = context_window

    @property
    def context_window(self) -> int:
        return self._context_window

    def count_tokens(self, messages: list[Any]) -> int:
        return sum(max(1, len(json.dumps(message, default=str)) // 4) for message in messages)

    def _format_messages(self, messages: list[dict]) -> list[dict]:
        """Convert generic harness message dicts to OpenAI's chat format.

        OpenAI requires:
          - assistant tool calls → role "assistant" with "tool_calls" list (function format)
          - tool results         → role "tool" with "tool_call_id"
        """
        result: list[dict] = []
        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls") or []
            tool_call_id = msg.get("tool_call_id")

            if role == "tool_result":
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id or "",
                        "content": content,
                    }
                )
            elif role == "assistant" and tool_calls:
                result.append(
                    {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": (
                                        json.dumps(tc["arguments"])
                                        if isinstance(tc["arguments"], dict)
                                        else tc["arguments"]
                                    ),
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )
            else:
                result.append({"role": role, "content": content})

        return result

    async def complete(
        self,
        messages: list[Any],
        tools: list[ToolSchema],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        signal: Any | None = None,
    ) -> AsyncIterator[Chunk]:
        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "OpenAIProvider requires the optional 'openai' extra"
            ) from exc

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        formatted = self._format_messages(messages)
        stream = await client.chat.completions.create(
            model=self.model,
            messages=formatted,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ]
            or None,
            stream=True,
        )
        async for chunk in stream:
            for choice in chunk.choices:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                if getattr(delta, "content", None):
                    yield Chunk(text=delta.content)
                for tool_call in getattr(delta, "tool_calls", []) or []:
                    function = getattr(tool_call, "function", None)
                    yield Chunk(
                        tool_call_id=getattr(tool_call, "id", None),
                        tool_name=getattr(function, "name", None),
                        tool_arguments=getattr(function, "arguments", None),
                    )
