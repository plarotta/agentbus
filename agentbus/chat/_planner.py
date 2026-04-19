"""ChatPlannerNode — wraps the Harness and routes tool calls through the bus."""

from __future__ import annotations

from agentbus.harness import Harness, Session
from agentbus.harness.providers import SystemPrompt, ToolSchema
from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.common import InboundChat, OutboundChat
from agentbus.schemas.common import ToolRequest as BusToolRequest
from agentbus.schemas.common import ToolResult as BusToolResult
from agentbus.schemas.harness import PlannerStatus, ToolCall
from agentbus.schemas.harness import ToolResult as HarnessToolResult

from ._config import ChatConfig
from ._tools import TOOL_SCHEMAS

_SYSTEM_PROMPT = (
    "You are a helpful AI assistant with access to tools. "
    "Use them whenever you need precise information or to perform actions. "
    "Be concise and direct."
)


def _make_provider(config: ChatConfig):
    """Instantiate the provider specified in ChatConfig.

    Eagerly checks that the required package is installed and raises a clear,
    actionable error before the bus starts — not on the first user message.
    """
    provider = config.provider
    model = config.model

    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ModuleNotFoundError:
            raise SystemExit(
                "Error: the 'anthropic' package is not installed.\n"
                "Install it with:  uv sync --extra anthropic"
            ) from None
        from agentbus.harness.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=model,
            system_prompt=SystemPrompt(static_prefix=_SYSTEM_PROMPT),
        )

    if provider == "openai":
        try:
            import openai  # noqa: F401
        except ModuleNotFoundError:
            raise SystemExit(
                "Error: the 'openai' package is not installed.\n"
                "Install it with:  uv sync --extra openai"
            ) from None
        from agentbus.harness.providers.openai import (
            OpenAIProvider,  # type: ignore[import-not-found]
        )

        return OpenAIProvider(model=model, system_prompt=SystemPrompt(static_prefix=_SYSTEM_PROMPT))

    if provider in ("ollama", "mlx"):
        try:
            import httpx  # noqa: F401
        except ModuleNotFoundError:
            raise SystemExit(
                "Error: the 'httpx' package is not installed (required for ollama/mlx).\n"
                "Install it with:  uv sync --extra ollama"
            ) from None
        from agentbus.harness.providers.ollama import OllamaProvider

        base_url = "http://localhost:11434"
        return OllamaProvider(model=model, base_url=base_url)

    raise SystemExit(
        f"Error: unknown provider {provider!r}.\nExpected one of: anthropic, openai, ollama, mlx"
    )


class ChatPlannerNode(Node):
    """PlannerNode for agentbus chat mode.

    Receives messages from /inbound, runs the LLM harness, routes all
    tool calls through the bus (via /tools/request and /tools/result),
    and publishes responses to /outbound.  Emits PlannerStatus updates
    to /planning/status throughout the loop.
    """

    name = "planner"
    subscriptions = ["/inbound"]
    publications = ["/outbound", "/tools/request", "/planning/status"]
    concurrency_mode = "serial"  # LLM loop is stateful — one message at a time

    def __init__(
        self,
        config: ChatConfig,
        session: Session | None = None,
        *,
        provider=None,
        extra_tools: list[ToolSchema] | None = None,
    ) -> None:
        self._config = config
        self._session = session or Session()
        self._bus = None
        self._harness: Harness | None = None
        self._iteration = 0
        self._injected_provider = provider  # for tests — bypasses _make_provider()
        self._extra_tools = list(extra_tools) if extra_tools else []

    @property
    def session(self) -> Session:
        return self._session

    @property
    def context_window(self) -> int | None:
        """Provider context window in tokens, once the harness is live."""
        if self._harness is None:
            return None
        return self._harness.context_window

    async def on_init(self, bus) -> None:
        self._bus = bus
        provider = (
            self._injected_provider
            if self._injected_provider is not None
            else _make_provider(self._config)
        )
        tools: list[ToolSchema] = [TOOL_SCHEMAS[t] for t in self._config.tools if t in TOOL_SCHEMAS]
        tools.extend(self._extra_tools)

        async def tool_executor(call: ToolCall) -> HarnessToolResult:
            await self._status("tool_dispatched", tool_name=call.name)
            reply: Message = await self._bus.request(
                "/tools/request",
                BusToolRequest(tool=call.name, params=call.arguments),
                reply_on="/tools/result",
                timeout=60.0,
            )
            await self._status("tool_received", tool_name=call.name)
            bus_result: BusToolResult = reply.payload
            return HarnessToolResult(
                tool_call_id=call.id,
                output=bus_result.output,
                error=bus_result.error,
            )

        self._harness = Harness(
            provider=provider,
            tool_executor=tool_executor,
            tools=tools,
            session=self._session,
        )

    async def _status(
        self, event: str, *, tool_name: str | None = None, detail: str | None = None
    ) -> None:
        self._iteration += 1
        await self._bus.publish(
            "/planning/status",
            PlannerStatus(
                event=event,
                iteration=self._iteration,
                context_tokens=0,
                context_capacity=0.0,
                tool_name=tool_name,
                detail=detail,
            ),
        )

    async def on_message(self, msg: Message) -> None:
        inbound: InboundChat = msg.payload
        await self._status("thinking")
        try:
            response = await self._harness.run(inbound.text)
            await self._status("responding")
        except Exception as exc:
            await self._status("error", detail=str(exc))
            response = f"I encountered an error: {exc}"
        await self._bus.publish(
            "/outbound",
            OutboundChat(
                text=response,
                reply_to=inbound.sender,
                channel=inbound.channel,
                metadata=dict(inbound.metadata),
            ),
        )
