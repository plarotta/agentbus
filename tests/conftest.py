import pytest

from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest, ToolResult
from agentbus.schemas.harness import ContentBlock, ConversationTurn, ToolCall


@pytest.fixture
def inbound_payload():
    return InboundChat(channel="cli", sender="user", text="hello")


@pytest.fixture
def tool_request_payload():
    return ToolRequest(tool="browser", action="navigate", params={"url": "https://example.com"})


@pytest.fixture
def tool_call():
    return ToolCall(id="call-1", name="browser", arguments={"url": "https://example.com"})
