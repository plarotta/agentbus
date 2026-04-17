from agentbus.harness.compaction import MAX_CONSECUTIVE_COMPACT_FAILURES, AutoCompact, MicroCompact
from agentbus.schemas.harness import ConversationTurn


async def test_microcompact_truncates_large_tool_output():
    compact = MicroCompact(max_tool_output_tokens=100)
    large_output = "x" * 500
    messages = [
        ConversationTurn(role="tool_result", content=large_output, token_count=200),
    ]

    compacted = await compact.compact(messages)

    assert "tool output truncated" in compacted[0].content


async def test_autocompact_circuit_breaker_stops_after_three_failures():
    calls = 0

    async def failing_summary(messages):
        nonlocal calls
        calls += 1
        raise RuntimeError("nope")

    compact = AutoCompact(failing_summary)
    messages = [ConversationTurn(role="user", content="hello", token_count=1)]

    for _ in range(MAX_CONSECUTIVE_COMPACT_FAILURES + 2):
        result = await compact.compact(messages)
        assert result.compacted is False

    assert calls == MAX_CONSECUTIVE_COMPACT_FAILURES
    assert compact.breaker.is_open
