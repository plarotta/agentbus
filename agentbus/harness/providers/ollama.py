import json
from typing import Any, AsyncIterator

from agentbus.harness.providers import Chunk, ToolSchema


def _format_messages_openai(messages: list[dict]) -> list[dict]:
    """Convert generic harness dicts to OpenAI-compatible format (used by Ollama)."""
    result: list[dict] = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls") or []
        tool_call_id = msg.get("tool_call_id")

        if role == "tool_result":
            result.append({"role": "tool", "tool_call_id": tool_call_id or "", "content": content})
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


class OllamaProvider:
    """Async Ollama provider.

    The implementation imports httpx lazily so the package remains importable
    without the optional provider extra installed.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://localhost:11434",
        context_window: int = 32_768,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._context_window = context_window

    @property
    def context_window(self) -> int:
        return self._context_window

    def count_tokens(self, messages: list[Any]) -> int:
        return sum(max(1, len(json.dumps(message, default=str)) // 4) for message in messages)

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
            import httpx
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "OllamaProvider requires the optional 'ollama' extra (httpx)"
            ) from exc

        payload = {
            "model": self.model,
            "messages": _format_messages_openai(messages),
            "stream": True,
            "options": {"temperature": temperature},
            "tools": [tool.__dict__ for tool in tools],
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        if stop:
            payload["options"]["stop"] = stop

        async with httpx.AsyncClient(base_url=self.base_url, timeout=60.0) as client:
            async with client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    message = data.get("message", {})
                    if message.get("content"):
                        yield Chunk(text=message["content"])
                    for tool_call in message.get("tool_calls", []):
                        function = tool_call.get("function", {})
                        yield Chunk(
                            tool_call_id=tool_call.get("id"),
                            tool_name=function.get("name"),
                            tool_arguments=function.get("arguments", "{}"),
                        )
