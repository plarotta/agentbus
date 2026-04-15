from collections.abc import Sequence

from agentbus.schemas.harness import ToolCall, ToolResult


class Extension:
    """Base harness extension with passthrough hooks."""

    def on_context(self, messages):
        return messages

    def on_before_llm(self, messages, tools):
        return messages, tools

    def on_tool_call(self, tool_call: ToolCall) -> ToolCall | None:
        return tool_call

    def on_tool_result(self, tool_call: ToolCall, result: ToolResult) -> ToolResult:
        return result

    def on_before_compact(self, messages):
        return messages

    def on_response(self, response: str) -> str:
        return response

    def on_error(self, error: Exception) -> str | None:
        return None


def run_on_context(messages, extensions: Sequence[Extension]):
    for extension in extensions:
        messages = extension.on_context(messages)
    return messages


def run_on_before_llm(messages, tools, extensions: Sequence[Extension]):
    for extension in extensions:
        messages, tools = extension.on_before_llm(messages, tools)
    return messages, tools


def run_on_tool_call(tool_call: ToolCall, extensions: Sequence[Extension]) -> ToolCall | None:
    current = tool_call
    for extension in extensions:
        if current is None:
            return None
        current = extension.on_tool_call(current)
    return current


def run_on_tool_result(
    tool_call: ToolCall,
    result: ToolResult,
    extensions: Sequence[Extension],
) -> ToolResult:
    current = result
    for extension in extensions:
        current = extension.on_tool_result(tool_call, current)
    return current


def run_on_before_compact(messages, extensions: Sequence[Extension]):
    current = messages
    for extension in extensions:
        updated = extension.on_before_compact(current)
        if updated is not None:
            current = updated
    return current


def run_on_response(response: str, extensions: Sequence[Extension]) -> str:
    current = response
    for extension in extensions:
        current = extension.on_response(current)
    return current


def run_on_error(error: Exception, extensions: Sequence[Extension]) -> str | None:
    for extension in extensions:
        fallback = extension.on_error(error)
        if fallback is not None:
            return fallback
    return None


__all__ = [
    "Extension",
    "run_on_before_compact",
    "run_on_before_llm",
    "run_on_context",
    "run_on_error",
    "run_on_response",
    "run_on_tool_call",
    "run_on_tool_result",
]
