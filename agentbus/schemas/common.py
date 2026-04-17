from pydantic import BaseModel, Field


class InboundChat(BaseModel):
    """Message arriving from an external channel (gateway or test harness)."""

    channel: str
    sender: str
    text: str
    metadata: dict = Field(default_factory=dict)


class OutboundChat(BaseModel):
    """Message leaving to an external channel.

    ``channel`` names the gateway that should send this message (``"slack"``,
    ``"telegram"``, etc.). When unset, every subscribing gateway will accept
    the message — useful for single-channel deployments. With multiple
    gateways, the planner echoes ``InboundChat.channel`` so the right gateway
    picks it up. ``metadata`` round-trips per-channel threading info
    (e.g. Slack ``thread_ts``, Telegram ``chat_id``).
    """

    text: str
    reply_to: str | None = None
    channel: str | None = None
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
