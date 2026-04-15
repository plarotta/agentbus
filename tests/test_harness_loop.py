from collections.abc import AsyncIterator

from agentbus.harness import Harness
from agentbus.harness.compaction import CompactResult
from agentbus.harness.providers import Chunk, ToolSchema
from agentbus.harness.session import Session
from agentbus.schemas.harness import ConversationTurn, ToolCall, ToolResult


class FakeDeps:
    def __init__(self, turns: list[list[Chunk]]) -> None:
        self.turns = list(turns)
        self.calls: list[dict] = []
        self.compactions = 0

    async def call_provider(
        self,
        messages: list[ConversationTurn],
        tools: list[ToolSchema],
        **kwargs,
    ) -> AsyncIterator[Chunk]:
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        chunks = self.turns.pop(0)

        async def _stream():
            for chunk in chunks:
                yield chunk

        return _stream()

    async def microcompact(self, messages: list[ConversationTurn]) -> list[ConversationTurn]:
        return messages

    async def autocompact(self, messages: list[ConversationTurn]) -> CompactResult:
        self.compactions += 1
        return CompactResult(messages=messages, compacted=False)

    def uuid(self) -> str:
        return "fake-session"


async def test_harness_loop_executes_tool_then_returns_text(tmp_path):
    deps = FakeDeps(
        [
            [Chunk(tool_call_id="call-1", tool_name="browser", tool_arguments='{"url":"https://example.com"}')],
            [Chunk(text="done")],
        ]
    )
    tool_calls: list[ToolCall] = []

    async def tool_executor(tool_call: ToolCall) -> ToolResult:
        tool_calls.append(tool_call)
        return ToolResult(tool_call_id=tool_call.id, output="example page")

    harness = Harness(
        deps=deps,
        tool_executor=tool_executor,
        tools=[ToolSchema(name="browser", description="Fetch a page")],
        session=Session("loop-1", root_dir=tmp_path),
    )

    result = await harness.run("look this up")

    assert result == "done"
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "browser"
    assert harness.session.file_path.exists()


async def test_assistant_turn_saved_before_tool_results(tmp_path):
    """Assistant turns with tool_calls must appear in session before tool_result turns."""
    deps = FakeDeps(
        [
            [Chunk(tool_call_id="call-1", tool_name="search", tool_arguments="{}")],
            [Chunk(text="all done")],
        ]
    )

    async def tool_executor(tool_call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=tool_call.id, output="result")

    harness = Harness(
        deps=deps,
        tool_executor=tool_executor,
        tools=[ToolSchema(name="search")],
        session=Session("history-1", root_dir=tmp_path),
    )

    await harness.run("search something")

    roles = [t.role for t in harness.session.turns]
    # user → assistant(tool_calls) → tool_result → assistant(text)
    assert roles == ["user", "assistant", "tool_result", "assistant"]
    assistant_with_calls = harness.session.turns[1]
    assert assistant_with_calls.tool_calls is not None
    assert assistant_with_calls.tool_calls[0].name == "search"


async def test_harness_forces_text_response_after_max_iterations(tmp_path):
    deps = FakeDeps(
        [
            [Chunk(tool_call_id="call-1", tool_name="browser", tool_arguments="{}")],
            [Chunk(tool_call_id="call-2", tool_name="browser", tool_arguments="{}")],
            [Chunk(text="forced response")],
        ]
    )
    tool_calls: list[ToolCall] = []

    async def tool_executor(tool_call: ToolCall) -> ToolResult:
        tool_calls.append(tool_call)
        return ToolResult(tool_call_id=tool_call.id, output="ok")

    harness = Harness(
        deps=deps,
        tool_executor=tool_executor,
        tools=[ToolSchema(name="browser")],
        session=Session("loop-2", root_dir=tmp_path),
        max_iterations=2,
    )

    result = await harness.run("keep going")

    assert result == "forced response"
    assert len(tool_calls) == 2
    assert deps.calls[-1]["tools"] == []
