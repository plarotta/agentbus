from agentbus.harness.extensions import (
    Extension,
    run_on_context,
    run_on_tool_call,
)
from agentbus.harness.providers import SystemPrompt
from agentbus.schemas.harness import ConversationTurn, ToolCall


def test_extensions_pipeline_chains_in_order():
    order: list[str] = []

    class First(Extension):
        def on_context(self, messages):
            order.append("first")
            return messages + [ConversationTurn(role="assistant", content="one", token_count=1)]

    class Second(Extension):
        def on_context(self, messages):
            order.append("second")
            return messages + [ConversationTurn(role="assistant", content="two", token_count=1)]

    messages = run_on_context(
        [ConversationTurn(role="user", content="hello", token_count=1)],
        [First(), Second()],
    )

    assert order == ["first", "second"]
    assert [turn.content for turn in messages] == ["hello", "one", "two"]


def test_extension_can_block_tool_call():
    class Blocker(Extension):
        def on_tool_call(self, tool_call):
            return None

    tool_call = ToolCall(id="call-1", name="browser", arguments={"url": "https://example.com"})
    assert run_on_tool_call(tool_call, [Blocker()]) is None


def test_system_prompt_render_shapes():
    prompt = SystemPrompt(static_prefix="static", dynamic_suffix="dynamic")
    rendered = prompt.render()

    assert rendered[0]["cache_control"] == {"type": "ephemeral"}
    assert prompt.render_plain() == "static\ndynamic"
