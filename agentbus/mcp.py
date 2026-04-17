"""MCP (Model Context Protocol) gateway.

Bridges external MCP tool servers onto the bus's ``/tools/request`` /
``/tools/result`` topics. Each configured server is spawned as a
stdio subprocess; its advertised tools are registered with the planner
under a namespaced name (``mcp__<server>__<tool>``) so MCP tools never
shadow builtins.

Lifecycle ownership is split:

* :func:`open_mcp_runtime` opens the stdio subprocesses inside the
  caller's task and returns an :class:`MCPRuntime`. The caller is
  responsible for ``await runtime.aclose()`` — this keeps the anyio
  cancel-scope entry/exit in the same task, which is a hard
  requirement of the underlying SDK.
* :class:`MCPGatewayNode` is the bus-facing part. It holds a
  reference to the runtime, subscribes to ``/tools/request``, and
  publishes results. It does *not* close the runtime on shutdown —
  that's the runner's job.

Requires the ``mcp`` package (install with ``uv sync --extra mcp``).
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from agentbus.harness.providers import ToolSchema
from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.common import ToolRequest
from agentbus.schemas.common import ToolResult as BusToolResult

logger = logging.getLogger(__name__)

_TOOL_PREFIX = "mcp__"
_NAME_SEP = "__"


def mcp_tool_name(server: str, tool: str) -> str:
    """Return the namespaced bus-facing name for an MCP tool."""
    return f"{_TOOL_PREFIX}{server}{_NAME_SEP}{tool}"


# ── Config ───────────────────────────────────────────────────────────────────


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None


def load_servers_from_dict(data: Any) -> list[MCPServerConfig]:
    """Parse the ``mcp_servers:`` section of ``agentbus.yaml``.

    Accepts a list of ``{name, command, args?, env?}`` mappings. Raises
    ``ValueError`` on malformed entries so the CLI can surface a clear
    startup error rather than failing mid-conversation.
    """
    if not data:
        return []
    if not isinstance(data, list):
        raise ValueError("mcp_servers must be a list of server configs")
    servers: list[MCPServerConfig] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("mcp_servers entry must be a mapping")
        name = entry.get("name")
        command = entry.get("command")
        if not name or not command:
            raise ValueError("mcp_servers entry requires 'name' and 'command'")
        servers.append(
            MCPServerConfig(
                name=name,
                command=command,
                args=list(entry.get("args", [])),
                env=dict(entry["env"]) if entry.get("env") else None,
            )
        )
    return servers


# ── Runtime ──────────────────────────────────────────────────────────────────


@dataclass
class _Binding:
    server: str
    tool_name: str
    session: Any  # mcp.ClientSession


@dataclass
class MCPRuntime:
    """Open MCP sessions, their bindings, and the owning exit stack.

    The caller must invoke ``await aclose()`` from the same task that
    called :func:`open_mcp_runtime`.
    """

    bindings: dict[str, _Binding]
    schemas: list[ToolSchema]
    _stack: AsyncExitStack

    async def aclose(self) -> None:
        await self._stack.aclose()

    def tool_schemas(self) -> list[ToolSchema]:
        return list(self.schemas)


def _require_mcp_sdk() -> None:
    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError:
        raise SystemExit(
            "Error: the 'mcp' package is not installed.\nInstall it with:  uv sync --extra mcp"
        ) from None


async def open_mcp_runtime(configs: list[MCPServerConfig]) -> MCPRuntime:
    """Spawn each MCP server subprocess, handshake, and list tools.

    Returns an :class:`MCPRuntime` whose ``bindings`` map namespaced
    tool names to the owning ``ClientSession``. On partial failure
    the already-opened subprocesses are torn down before the exception
    propagates.
    """
    _require_mcp_sdk()
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    stack = AsyncExitStack()
    bindings: dict[str, _Binding] = {}
    schemas: list[ToolSchema] = []
    try:
        for srv in configs:
            params = StdioServerParameters(command=srv.command, args=srv.args, env=srv.env)
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            for tool in listed.tools:
                name = mcp_tool_name(srv.name, tool.name)
                bindings[name] = _Binding(server=srv.name, tool_name=tool.name, session=session)
                schemas.append(
                    ToolSchema(
                        name=name,
                        description=tool.description or "",
                        input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                    )
                )
            logger.info("mcp server %r initialized with %d tools", srv.name, len(listed.tools))
    except Exception:
        await stack.aclose()
        raise
    return MCPRuntime(bindings=bindings, schemas=schemas, _stack=stack)


# ── Gateway node ─────────────────────────────────────────────────────────────


def _render_mcp_content(blocks: list[Any]) -> str:
    """Flatten MCP CallToolResult content blocks into a single string.

    The MCP SDK returns typed content (TextContent, ImageContent, etc).
    The planner only consumes plain strings, so we stringify — text
    blocks pass through, images become a placeholder, anything else
    falls back to ``repr``.
    """
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
            continue
        type_ = getattr(block, "type", None)
        if type_ == "image":
            parts.append("[image]")
        else:
            parts.append(str(block))
    return "\n".join(parts)


class MCPGatewayNode(Node):
    """Route /tools/request to MCP sessions and publish /tools/result.

    Only handles requests for tools it owns — unknown tool names are
    silently dropped so a co-subscribed :class:`ChatToolNode` (or
    another gateway) can respond. The planner is the source of truth
    for which tools are callable, so silent drop is safe: the LLM
    cannot invent tool names that no one registered.
    """

    name = "mcp_gateway"
    subscriptions = ["/tools/request"]
    publications = ["/tools/result"]

    def __init__(self, runtime: MCPRuntime) -> None:
        self._runtime = runtime
        self._bus: Any = None

    async def on_init(self, bus: Any) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        request: ToolRequest = msg.payload
        binding = self._runtime.bindings.get(request.tool)
        if binding is None:
            return

        try:
            result = await binding.session.call_tool(binding.tool_name, arguments=request.params)
        except Exception as exc:
            logger.exception(
                "mcp call failed: server=%s tool=%s", binding.server, binding.tool_name
            )
            await self._publish_result(msg, output=None, error=str(exc))
            return

        content = _render_mcp_content(getattr(result, "content", []) or [])
        is_error = bool(getattr(result, "isError", False))
        if is_error:
            await self._publish_result(msg, output=None, error=content or "mcp error")
        else:
            await self._publish_result(msg, output=content, error=None)

    async def _publish_result(self, msg: Message, *, output: str | None, error: str | None) -> None:
        await self._bus.publish(
            "/tools/result",
            BusToolResult(tool_call_id=msg.id, output=output, error=error),
            correlation_id=msg.correlation_id,
        )


__all__ = [
    "MCPGatewayNode",
    "MCPRuntime",
    "MCPServerConfig",
    "load_servers_from_dict",
    "mcp_tool_name",
    "open_mcp_runtime",
]
