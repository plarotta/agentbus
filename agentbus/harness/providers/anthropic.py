import json
from collections.abc import AsyncIterator
from typing import Any

from agentbus.harness.providers import Chunk, SystemPrompt, ToolSchema


class AnthropicProvider:
    """Anthropic streaming provider."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        context_window: int = 200_000,
        system_prompt: SystemPrompt | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self._context_window = context_window
        self.system_prompt = system_prompt

    @property
    def context_window(self) -> int:
        return self._context_window

    def count_tokens(self, messages: list[Any]) -> int:
        return sum(max(1, len(json.dumps(message, default=str)) // 4) for message in messages)

    def _format_messages(self, messages: list[dict]) -> list[dict]:
        """Convert generic harness message dicts to Anthropic's content-block format.

        Anthropic requires:
          - assistant tool calls  → content blocks of type "tool_use"
          - tool results          → role "user" with content blocks of type "tool_result"
                                    (consecutive results are grouped into one user message)
        """
        result: list[dict] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg["role"]

            if role == "tool_result":
                # Collect all consecutive tool_result turns into one user message.
                tool_result_blocks: list[dict] = []
                while i < len(messages) and messages[i]["role"] == "tool_result":
                    m = messages[i]
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", ""),
                            "content": m.get("content", ""),
                        }
                    )
                    i += 1
                result.append({"role": "user", "content": tool_result_blocks})

            elif role == "assistant":
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls") or []
                if tool_calls:
                    blocks: list[dict] = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc["id"],
                                "name": tc["name"],
                                "input": tc["arguments"],
                            }
                        )
                    result.append({"role": "assistant", "content": blocks})
                else:
                    result.append({"role": "assistant", "content": content})
                i += 1

            else:  # user
                result.append({"role": "user", "content": msg.get("content", "")})
                i += 1

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
            import anthropic
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "AnthropicProvider requires the optional 'anthropic' extra"
            ) from exc

        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        system = self.system_prompt.render() if self.system_prompt else None
        formatted = self._format_messages(messages)
        stream = await client.messages.create(
            model=self.model,
            system=system,
            messages=formatted,
            max_tokens=max_tokens or 1024,
            temperature=temperature,
            stop_sequences=stop,
            tools=[
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ],
            stream=True,
        )

        # Track in-progress tool_use blocks by content block index.
        # Anthropic streams tool calls as:
        #   content_block_start  → id, name (input is {})
        #   content_block_delta  → input_json_delta fragments
        #   content_block_stop   → signals the block is complete
        tool_blocks: dict[int, dict] = {}

        async with stream as events:
            async for event in events:
                if event.type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if getattr(block, "type", None) == "tool_use":
                        tool_blocks[event.index] = {
                            "id": block.id,
                            "name": block.name,
                            "json_parts": [],
                        }
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if getattr(delta, "type", None) == "text_delta" and getattr(
                        delta, "text", None
                    ):
                        yield Chunk(text=delta.text)
                    elif (
                        getattr(delta, "type", None) == "input_json_delta"
                        and event.index in tool_blocks
                    ):
                        tool_blocks[event.index]["json_parts"].append(delta.partial_json)
                elif event.type == "content_block_stop":
                    tb = tool_blocks.pop(event.index, None)
                    if tb is not None:
                        yield Chunk(
                            tool_call_id=tb["id"],
                            tool_name=tb["name"],
                            tool_arguments="".join(tb["json_parts"]),
                        )
