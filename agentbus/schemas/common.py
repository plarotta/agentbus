from pydantic import BaseModel, Field


class InboundChat(BaseModel):
    """Message arriving from an external channel (gateway or test harness)."""

    channel: str
    sender: str
    text: str
    metadata: dict = Field(default_factory=dict)


class OutboundChat(BaseModel):
    """Message leaving to an external channel."""

    text: str
    reply_to: str | None = None
    metadata: dict = Field(default_factory=dict)


class ToolRequest(BaseModel):
    """Tool execution request published to /tools/request by a planner node."""

    tool: str
    action: str | None = None
    params: dict = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Tool execution result published to /tools/result by a tool node."""

    tool_call_id: str
    output: str | None = None
    error: str | None = None
