"""Workflow-builder helper agent.

An LLM assistant that designs workflows on the user's behalf — it creates topics
(with generated Pydantic schemas), creates nodes (authoring their hook bodies),
edits hooks, and applies changes by reloading the bus. All mutations go through
the same ``WorkflowManager`` the REST endpoints use, so the two can't drift.

It is built on the bus-free ``Harness`` (per the harness-isolation invariant): a
plain ``tool_executor`` callback dispatches the builder tools straight against the
``WorkflowManager`` + ``BusSupervisor``. No second planner runs inside the user's
workflow bus. Provider packages are validated eagerly (fail-fast) when the agent
is first constructed, never mid-conversation.
"""

import dataclasses
import json
from typing import Any

from agentbus.harness import Harness, Session
from agentbus.harness.providers import SystemPrompt, ToolSchema
from agentbus.schemas.harness import ToolCall
from agentbus.schemas.harness import ToolResult as HarnessToolResult

_BUILDER_PROMPT = (
    "You are the AgentBus workflow builder. You help the user design a pub/sub "
    "workflow made of typed topics and nodes.\n"
    "- A topic is a named channel with a Pydantic schema (its message shape).\n"
    "- A node is a Python class that subscribes to topics, reacts in its "
    "`on_message` hook, and publishes to other topics.\n"
    "Use `read_workflow` and `inspect_graph` to see current state before "
    "changing things. When you create a node, write a concise, correct "
    "`on_message` body (the message arrives as `msg`; `msg.payload` is the typed "
    "model; `self.logger` and `self.publish(topic, payload)` are available — "
    "publish is a coroutine, so `await self.publish(...)`). After making changes, "
    "call `apply_changes` to (re)start the bus so they take effect. Be concise."
)

DEFAULT_BUILDER_MODEL = "claude-haiku-4-5-20251001"


BUILDER_TOOL_SCHEMAS: dict[str, ToolSchema] = {
    "read_workflow": ToolSchema(
        name="read_workflow",
        description="Return the current agentbus.yaml config (topics + nodes).",
        input_schema={"type": "object", "properties": {}},
    ),
    "inspect_graph": ToolSchema(
        name="inspect_graph",
        description="Return the live bus graph (running nodes, topics, edges).",
        input_schema={"type": "object", "properties": {}},
    ),
    "create_topic": ToolSchema(
        name="create_topic",
        description=(
            "Create a topic. Either pass `schema` (an existing 'module:Class' "
            "import path) OR `schema_class` + `fields` to generate a new Pydantic "
            "model. `fields` is a list of {name, type, required, default}; type is "
            "one of str/int/float/bool/list/dict."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Topic path, e.g. /orders"},
                "schema": {"type": "string"},
                "schema_class": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "object"}},
                "retention": {"type": "integer"},
                "description": {"type": "string"},
            },
            "required": ["name"],
        },
    ),
    "create_node": ToolSchema(
        name="create_node",
        description=(
            "Create a node. Generates a Node subclass with the given hook bodies "
            "and registers it. `on_message` is the body run per message."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "class_name": {"type": "string", "description": "PascalCase class name"},
                "node_name": {"type": "string", "description": "Unique bus node name"},
                "subscriptions": {"type": "array", "items": {"type": "string"}},
                "publications": {"type": "array", "items": {"type": "string"}},
                "on_message": {"type": "string", "description": "Python body for on_message"},
                "on_init": {"type": "string"},
                "on_shutdown": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["class_name", "node_name"],
        },
    ),
    "set_hook": ToolSchema(
        name="set_hook",
        description="Replace a hook body (on_init/on_message/on_shutdown) of a generated node.",
        input_schema={
            "type": "object",
            "properties": {
                "class_name": {"type": "string"},
                "hook": {"type": "string", "enum": ["on_init", "on_message", "on_shutdown"]},
                "body": {"type": "string"},
            },
            "required": ["class_name", "hook", "body"],
        },
    ),
    "apply_changes": ToolSchema(
        name="apply_changes",
        description="Rebuild and restart the bus so config/code changes take effect.",
        input_schema={"type": "object", "properties": {}},
    ),
}


def _make_builder_provider(provider_name: str, model: str) -> tuple[Any, bool]:
    """Provider factory; fail-fast on missing deps.

    Returns ``(provider, prepend_prompt)``. Only Anthropic carries the builder
    system prompt natively; for the others the second element is True so the
    caller prepends it to the first message (same approach as ``swarm.py``).
    """
    if provider_name == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "The builder agent needs the 'anthropic' package: uv sync --extra anthropic"
            ) from exc
        from agentbus.harness.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            model=model, system_prompt=SystemPrompt(static_prefix=_BUILDER_PROMPT)
        ), False

    if provider_name == "openai":
        try:
            import openai  # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "The builder agent needs the 'openai' package: uv sync --extra openai"
            ) from exc
        from agentbus.harness.providers.openai import OpenAIProvider

        return OpenAIProvider(model=model), True

    if provider_name in ("ollama", "mlx"):
        try:
            import httpx  # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "The builder agent needs 'httpx' for ollama/mlx: uv sync --extra ollama"
            ) from exc
        from agentbus.harness.providers.ollama import OllamaProvider

        return OllamaProvider(model=model, base_url="http://localhost:11434"), True

    raise RuntimeError(f"unknown provider {provider_name!r}")


class WorkflowBuilder:
    """Stateful builder conversation backed by a single Harness session."""

    def __init__(
        self,
        manager: Any,
        supervisor: Any,
        *,
        provider_name: str = "anthropic",
        model: str = DEFAULT_BUILDER_MODEL,
        provider: Any = None,
        harness: Harness | None = None,
    ) -> None:
        self.manager = manager
        self.supervisor = supervisor
        self.session = Session()
        self._pending_prefix: str | None = None
        if harness is not None:
            self._harness = harness
        else:
            if provider is not None:
                prov: Any = provider
            else:
                prov, prepend = _make_builder_provider(provider_name, model)
                if prepend:
                    self._pending_prefix = _BUILDER_PROMPT
            self._harness = Harness(
                provider=prov,
                tool_executor=self._execute,
                tools=list(BUILDER_TOOL_SCHEMAS.values()),
                session=self.session,
            )

    async def chat(self, message: str) -> str:
        # Providers without native system-prompt support get the builder prompt
        # prepended to the first turn only.
        if self._pending_prefix is not None:
            message = f"{self._pending_prefix}\n\n{message}"
            self._pending_prefix = None
        return await self._harness.run(message)

    async def _execute(self, call: ToolCall) -> HarnessToolResult:
        try:
            output = await self._dispatch(call.name, dict(call.arguments))
            return HarnessToolResult(tool_call_id=call.id, output=output)
        except Exception as exc:
            return HarnessToolResult(tool_call_id=call.id, error=str(exc))

    async def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name == "read_workflow":
            return json.dumps(self.manager.read_config())
        if name == "inspect_graph":
            bus = self.supervisor.bus
            if bus is None:
                return "{}"
            return json.dumps(dataclasses.asdict(bus.graph()))
        if name == "create_topic":
            entry = self.manager.create_topic(**args)
            return f"Created topic {entry['name']} (schema {entry['schema']})"
        if name == "create_node":
            self.manager.create_node(**args)
            return f"Created node {args.get('node_name')!r}"
        if name == "set_hook":
            self.manager.set_hook(args["class_name"], args["hook"], args["body"])
            return f"Updated {args['hook']} on {args['class_name']}"
        if name == "apply_changes":
            await self.supervisor.reload()
            return "Applied — bus rebuilt and restarted."
        return f"unknown tool {name!r}"


__all__ = ["BUILDER_TOOL_SCHEMAS", "DEFAULT_BUILDER_MODEL", "WorkflowBuilder"]
