"""
Tool-using agent example — standalone Harness with Anthropic.

Demonstrates:
  - Defining tools with ToolSchema
  - Implementing a tool_executor callback
  - Running multi-turn conversations with session persistence
  - Reading the saved session from disk

Setup:
    uv sync --extra anthropic

Usage:
    ANTHROPIC_API_KEY=sk-ant-... uv run python examples/tool_agent/main.py

Optional env vars:
    MODEL   — Anthropic model ID (default: claude-haiku-4-5-20251001)
"""

import asyncio
import math
import os
from datetime import datetime, timezone

from agentbus.harness import Harness, Session
from agentbus.harness.providers import SystemPrompt, ToolSchema
from agentbus.harness.providers.anthropic import AnthropicProvider
from agentbus.schemas.harness import ToolCall, ToolResult

# ---------------------------------------------------------------------------
# Tool definitions — passed to the LLM so it knows what's available
# ---------------------------------------------------------------------------

TOOLS = [
    ToolSchema(
        name="calculate",
        description=(
            "Evaluate a safe mathematical expression using Python math. "
            "Supports arithmetic operators and all functions in the math module "
            "(sqrt, log, sin, cos, ceil, floor, …)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "A math expression, e.g. '2 ** 10' or 'math.sqrt(2)'",
                }
            },
            "required": ["expression"],
        },
    ),
    ToolSchema(
        name="get_time",
        description="Return the current date and time in UTC.",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSchema(
        name="word_count",
        description="Count words and characters in a piece of text.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to analyze."}
            },
            "required": ["text"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Tool executor — called by the Harness whenever the LLM invokes a tool
# ---------------------------------------------------------------------------

async def execute_tool(call: ToolCall) -> ToolResult:
    """Dispatch a ToolCall to the matching implementation."""
    try:
        match call.name:
            case "calculate":
                expr = call.arguments.get("expression", "")
                # Restricted eval: only math module, no builtins.
                result = eval(expr, {"__builtins__": {}}, {"math": math})  # noqa: S307
                return ToolResult(tool_call_id=call.id, output=str(result))

            case "get_time":
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                return ToolResult(tool_call_id=call.id, output=now)

            case "word_count":
                text = call.arguments.get("text", "")
                words = len(text.split())
                chars = len(text)
                return ToolResult(
                    tool_call_id=call.id,
                    output=f"{words} words, {chars} characters",
                )

            case _:
                return ToolResult(tool_call_id=call.id, error=f"unknown tool: {call.name}")

    except Exception as exc:  # noqa: BLE001
        return ToolResult(tool_call_id=call.id, error=f"tool error: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    model = os.environ.get("MODEL", "claude-haiku-4-5-20251001")
    print(f"Using model: {model}\n")

    provider = AnthropicProvider(
        model=model,
        system_prompt=SystemPrompt(
            static_prefix=(
                "You are a helpful assistant with access to tools. "
                "Use them when you need precise answers, then respond concisely."
            )
        ),
    )

    session = Session()  # auto-generates a session_id; persists to ~/.agentbus/sessions/

    harness = Harness(
        provider=provider,
        tool_executor=execute_tool,
        tools=TOOLS,
        session=session,
    )

    # Run a few questions. The Harness maintains conversation history across calls.
    questions = [
        "What is 2 raised to the power of 32?",
        "What is the square root of that result?",  # uses prior context
        "What time is it right now?",
        "Count the words in: 'The quick brown fox jumps over the lazy dog'",
    ]

    for question in questions:
        print(f"User: {question}")
        response = await harness.run(question)
        print(f"Assistant: {response}\n")

    print(f"Session saved → {harness.session.file_path}")
    print(f"Total turns:    {len(harness.session.turns)}")
    print(f"Total tokens:   {harness.session.total_tokens()}")


if __name__ == "__main__":
    asyncio.run(main())
