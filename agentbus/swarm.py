"""Hub-and-spoke multi-agent orchestration.

A coordinator LLM exposes a ``dispatch_subagent`` tool; sub-agents live on
namespaced topics (``/swarm/<name>/inbound`` and ``/swarm/<name>/outbound``)
and run a fresh :class:`~agentbus.harness.Harness` per dispatch. The
coordinator talks only to the dispatcher tool; sub-agents never talk to
each other. That is the "hub-and-spoke" part — handoffs go through the
coordinator, not between peers.

Why this shape:

* **Isolation by topic namespace.** Each sub-agent gets its own pair of
  topics. Observation (``agentbus topic echo /swarm/researcher/outbound``)
  and history replay work without special casing.
* **Bus-mediated dispatch.** :class:`SwarmCoordinatorNode` routes with
  ``bus.request()`` so the existing correlation-ID plumbing returns the
  right reply to the right tool-call future. Sub-agents echo
  ``msg.correlation_id`` on their outbound publish.
* **One-shot per dispatch.** Each inbound message to a sub-agent spins up
  a new :class:`~agentbus.harness.Session` and :class:`~agentbus.harness.Harness`.
  Sub-agents are stateless across dispatches, matching the
  Task-tool-style "fresh context" model — the coordinator owns the
  long-running conversation.

The module is deliberately small. A future mesh version (sub-agents that
talk to each other) would need either a shared broker or per-pair
authorizations, neither of which is worth the complexity for v1.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from agentbus.chat._config import ChatConfig
from agentbus.chat._tools import TOOL_SCHEMAS
from agentbus.harness import Harness, Session
from agentbus.harness.providers import SystemPrompt, ToolSchema
from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.common import InboundChat, OutboundChat
from agentbus.schemas.common import ToolRequest as BusToolRequest
from agentbus.schemas.common import ToolResult as BusToolResult
from agentbus.schemas.harness import ToolCall
from agentbus.schemas.harness import ToolResult as HarnessToolResult
from agentbus.topic import Topic

if TYPE_CHECKING:
    from agentbus.bus import MessageBus

DISPATCH_TOOL_NAME = "dispatch_subagent"
DEFAULT_DISPATCH_TIMEOUT_S = 120.0


@dataclass(slots=True)
class SubAgentSpec:
    """Declarative description of a single sub-agent.

    ``name`` is used as the topic-namespace component, so it must be URL-
    safe (no slashes). ``description`` is shown to the coordinator LLM in
    the dispatch tool's schema — make it a one-liner of when to pick this
    agent over another.
    """

    name: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    model: str | None = None


def _make_swarm_provider(config: ChatConfig, system_prompt: str) -> Any:
    """Provider factory that attaches a per-sub-agent system prompt.

    For anthropic, the system prompt is set on the provider (the path used
    by ``ChatPlannerNode``). For other providers, the prompt is attached
    best-effort via attribute assignment — if the provider doesn't carry
    system prompts, the sub-agent's prompt is prepended to the task text
    at dispatch time instead (see :meth:`SwarmAgentNode.on_message`).
    """
    provider_name = config.provider
    model = config.model

    if provider_name == "anthropic":
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
            system_prompt=SystemPrompt(static_prefix=system_prompt),
        )

    if provider_name == "openai":
        try:
            import openai  # noqa: F401
        except ModuleNotFoundError:
            raise SystemExit(
                "Error: the 'openai' package is not installed.\n"
                "Install it with:  uv sync --extra openai"
            ) from None
        from agentbus.harness.providers.openai import OpenAIProvider

        return OpenAIProvider(model=model)

    if provider_name in ("ollama", "mlx"):
        try:
            import httpx  # noqa: F401
        except ModuleNotFoundError:
            raise SystemExit(
                "Error: the 'httpx' package is not installed (required for ollama/mlx).\n"
                "Install it with:  uv sync --extra ollama"
            ) from None
        from agentbus.harness.providers.ollama import OllamaProvider

        return OllamaProvider(model=model, base_url="http://localhost:11434")

    raise SystemExit(
        f"Error: unknown provider {provider_name!r}.\n"
        "Expected one of: anthropic, openai, ollama, mlx"
    )


class SwarmAgentNode(Node):
    """One-shot sub-agent. Runs a fresh Harness per inbound message.

    Subscribes to ``/swarm/<spec.name>/inbound`` and publishes the final
    assistant response to ``/swarm/<spec.name>/outbound`` with the
    correlation ID echoed back — that is what unblocks
    :meth:`SwarmCoordinatorNode`'s ``bus.request`` future.

    Tool calls go through the normal ``/tools/request`` → ``/tools/result``
    bridge, same as :class:`ChatPlannerNode`. The sub-agent's declared
    tools are a *subset* of the ones registered with
    :class:`~agentbus.chat._tools.ChatToolNode` (or an MCP gateway): the
    tool schema list controls what the LLM is *told* about; the tool node
    decides what actually runs.
    """

    concurrency_mode = "serial"

    def __init__(
        self,
        spec: SubAgentSpec,
        config: ChatConfig,
        *,
        provider: Any = None,
    ) -> None:
        self._spec = spec
        self._config = config
        self._injected_provider = provider
        self.name = f"swarm-{spec.name}"  # type: ignore[misc]
        self._inbound_topic = f"/swarm/{spec.name}/inbound"
        self._outbound_topic = f"/swarm/{spec.name}/outbound"
        self.subscriptions = [self._inbound_topic]  # type: ignore[misc]
        self.publications = [self._outbound_topic, "/tools/request"]  # type: ignore[misc]
        self._bus: Any = None
        self._provider: Any = None
        self._tools: list[ToolSchema] = []
        self._prepend_system_prompt = False

    async def on_init(self, bus: Any) -> None:
        self._bus = bus
        if self._injected_provider is not None:
            self._provider = self._injected_provider
        else:
            effective_config = self._config
            if self._spec.model:
                effective_config = replace(self._config, model=self._spec.model)
            self._provider = _make_swarm_provider(effective_config, self._spec.system_prompt)
        self._prepend_system_prompt = not hasattr(self._provider, "system_prompt")
        self._tools = [TOOL_SCHEMAS[t] for t in self._spec.tools if t in TOOL_SCHEMAS]

    async def on_message(self, msg: Message) -> None:
        inbound: InboundChat = msg.payload
        session = Session()

        async def tool_executor(call: ToolCall) -> HarnessToolResult:
            reply: Message = await self._bus.request(
                "/tools/request",
                BusToolRequest(tool=call.name, params=call.arguments),
                reply_on="/tools/result",
                timeout=60.0,
            )
            bus_result: BusToolResult = reply.payload
            return HarnessToolResult(
                tool_call_id=call.id,
                output=bus_result.output,
                error=bus_result.error,
            )

        harness = Harness(
            provider=self._provider,
            tool_executor=tool_executor,
            tools=self._tools,
            session=session,
        )

        prompt = inbound.text
        if self._prepend_system_prompt:
            prompt = f"[system]\n{self._spec.system_prompt}\n\n[task]\n{inbound.text}"

        try:
            response = await harness.run(prompt)
        except Exception as exc:
            response = f"Sub-agent {self._spec.name!r} error: {exc}"

        await self._bus.publish(
            self._outbound_topic,
            OutboundChat(text=response, reply_to=self._spec.name),
            correlation_id=msg.correlation_id,
        )


class SwarmCoordinatorNode(Node):
    """Owns the ``dispatch_subagent`` tool.

    Subscribes to ``/tools/request`` alongside :class:`ChatToolNode` and
    any MCP gateway; silently drops tool requests it doesn't own so the
    three compose cleanly. When a ``dispatch_subagent`` call arrives, it
    publishes an :class:`InboundChat` to the selected sub-agent's
    ``/swarm/<name>/inbound`` topic via ``bus.request``, awaits the
    reply on ``/swarm/<name>/outbound``, and echoes it back as a
    ``ToolResult`` on ``/tools/result``.

    Unknown-agent and timeout errors surface as ``ToolResult.error``
    instead of raising — the coordinator LLM sees them like any other
    tool failure and can recover.
    """

    name = "swarm-coordinator"
    subscriptions = ["/tools/request"]

    def __init__(
        self,
        specs: list[SubAgentSpec],
        *,
        timeout_s: float = DEFAULT_DISPATCH_TIMEOUT_S,
    ) -> None:
        self._specs = {s.name: s for s in specs}
        self._timeout = timeout_s
        self._bus: Any = None
        self.publications = ["/tools/result"] + [  # type: ignore[misc]
            f"/swarm/{name}/inbound" for name in self._specs
        ]

    async def on_init(self, bus: Any) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        req: BusToolRequest = msg.payload
        if req.tool != DISPATCH_TOOL_NAME:
            return
        agent_name = req.params.get("agent")
        task = req.params.get("task")
        if not isinstance(agent_name, str) or agent_name not in self._specs:
            await self._reply(
                msg,
                error=(f"unknown sub-agent {agent_name!r}; available: {sorted(self._specs)}"),
            )
            return
        if not isinstance(task, str) or not task.strip():
            await self._reply(msg, error="'task' must be a non-empty string")
            return

        inbound_topic = f"/swarm/{agent_name}/inbound"
        outbound_topic = f"/swarm/{agent_name}/outbound"
        try:
            reply = await self._bus.request(
                inbound_topic,
                InboundChat(channel="swarm", sender=self.name, text=task),
                reply_on=outbound_topic,
                timeout=self._timeout,
            )
        except Exception as exc:
            await self._reply(msg, error=f"sub-agent {agent_name!r} failed: {exc}")
            return
        payload = reply.payload
        text = getattr(payload, "text", None) or str(payload)
        await self._reply(msg, output=text)

    async def _reply(
        self, msg: Message, *, output: str | None = None, error: str | None = None
    ) -> None:
        await self._bus.publish(
            "/tools/result",
            BusToolResult(tool_call_id=msg.id, output=output, error=error),
            correlation_id=msg.correlation_id,
        )


def build_dispatch_tool_schema(specs: list[SubAgentSpec]) -> ToolSchema:
    """Construct the ``dispatch_subagent`` :class:`ToolSchema` for these specs.

    The per-agent descriptions are inlined into the tool's description so
    the coordinator LLM can pick the right agent without additional
    round-trips. The ``agent`` parameter uses a JSON-schema ``enum`` for
    the same reason — ill-formed dispatches short-circuit at the provider
    layer.
    """
    agents_desc = "\n".join(f"- {s.name}: {s.description}" for s in specs)
    return ToolSchema(
        name=DISPATCH_TOOL_NAME,
        description=(
            "Hand off a scoped task to one of the available sub-agents and "
            "wait for its final answer. Each dispatch runs in a fresh "
            "context — the sub-agent cannot see prior conversation.\n\n"
            "Available sub-agents:\n"
            f"{agents_desc}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": [s.name for s in specs],
                    "description": "Which sub-agent to invoke.",
                },
                "task": {
                    "type": "string",
                    "description": "Self-contained task description for the sub-agent.",
                },
            },
            "required": ["agent", "task"],
        },
    )


def register_swarm(
    bus: MessageBus,
    specs: list[SubAgentSpec],
    config: ChatConfig,
    *,
    timeout_s: float = DEFAULT_DISPATCH_TIMEOUT_S,
    provider: Any = None,
) -> ToolSchema:
    """Register the topics, sub-agent nodes, and coordinator for a swarm.

    Returns the :class:`ToolSchema` the caller should pass to the
    coordinator planner as an extra tool. Call this *before* ``bus.spin()``.

    ``provider`` is a test hook: when set, every sub-agent uses it
    instead of calling :func:`_make_swarm_provider`. Production callers
    leave it as ``None``.
    """
    if not specs:
        raise ValueError("register_swarm requires at least one SubAgentSpec")
    seen: set[str] = set()
    for s in specs:
        if s.name in seen:
            raise ValueError(f"duplicate sub-agent name: {s.name!r}")
        seen.add(s.name)

    for spec in specs:
        bus.register_topic(Topic[InboundChat](f"/swarm/{spec.name}/inbound", retention=20))
        bus.register_topic(Topic[OutboundChat](f"/swarm/{spec.name}/outbound", retention=20))
    for spec in specs:
        bus.register_node(SwarmAgentNode(spec, config, provider=provider))
    bus.register_node(SwarmCoordinatorNode(specs, timeout_s=timeout_s))

    return build_dispatch_tool_schema(specs)


__all__ = [
    "DEFAULT_DISPATCH_TIMEOUT_S",
    "DISPATCH_TOOL_NAME",
    "SubAgentSpec",
    "SwarmAgentNode",
    "SwarmCoordinatorNode",
    "build_dispatch_tool_schema",
    "register_swarm",
]
