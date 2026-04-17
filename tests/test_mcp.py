"""Tests for agentbus.mcp — config parsing, gateway routing, content rendering."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import pytest

from agentbus.mcp import (
    MCPGatewayNode,
    MCPRuntime,
    _Binding,
    _render_mcp_content,
    load_servers_from_dict,
    mcp_tool_name,
)

# ── Config parsing ───────────────────────────────────────────────────────────


class TestLoadServersFromDict:
    def test_empty_returns_empty_list(self):
        assert load_servers_from_dict(None) == []
        assert load_servers_from_dict([]) == []

    def test_single_server(self):
        servers = load_servers_from_dict(
            [{"name": "fs", "command": "npx", "args": ["-y", "@mcp/fs"]}]
        )
        assert len(servers) == 1
        assert servers[0].name == "fs"
        assert servers[0].command == "npx"
        assert servers[0].args == ["-y", "@mcp/fs"]
        assert servers[0].env is None

    def test_env_is_copied(self):
        servers = load_servers_from_dict([{"name": "x", "command": "y", "env": {"FOO": "bar"}}])
        assert servers[0].env == {"FOO": "bar"}

    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="must be a list"):
            load_servers_from_dict({"name": "x", "command": "y"})

    def test_rejects_non_mapping_entry(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            load_servers_from_dict(["not-a-dict"])

    def test_rejects_missing_name(self):
        with pytest.raises(ValueError, match="requires 'name'"):
            load_servers_from_dict([{"command": "y"}])

    def test_rejects_missing_command(self):
        with pytest.raises(ValueError, match="requires 'name'"):
            load_servers_from_dict([{"name": "x"}])


# ── Namespacing ──────────────────────────────────────────────────────────────


class TestNameSpacing:
    def test_mcp_tool_name_format(self):
        assert mcp_tool_name("fs", "read_file") == "mcp__fs__read_file"

    def test_double_underscore_separator(self):
        name = mcp_tool_name("srv", "t")
        assert name.startswith("mcp__")
        assert "__" in name[5:]


# ── Content rendering ────────────────────────────────────────────────────────


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeImageBlock:
    type: str = "image"


class TestRenderMCPContent:
    def test_single_text(self):
        assert _render_mcp_content([_FakeTextBlock("hello")]) == "hello"

    def test_multiple_text_joined_with_newline(self):
        out = _render_mcp_content([_FakeTextBlock("a"), _FakeTextBlock("b")])
        assert out == "a\nb"

    def test_image_block_becomes_placeholder(self):
        out = _render_mcp_content([_FakeImageBlock()])
        assert out == "[image]"

    def test_empty_list_returns_empty_string(self):
        assert _render_mcp_content([]) == ""


# ── MCPGatewayNode routing ───────────────────────────────────────────────────


class _FakeSession:
    """Mocks the subset of mcp.ClientSession MCPGatewayNode uses."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._responses = responses or {}
        self.fail_with: Exception | None = None

    async def call_tool(self, name: str, *, arguments: dict) -> Any:
        self.calls.append((name, arguments))
        if self.fail_with is not None:
            raise self.fail_with
        return self._responses.get(name, _FakeCallResult(content=[_FakeTextBlock("ok")]))


@dataclass
class _FakeCallResult:
    content: list[Any]
    isError: bool = False


def _make_runtime(bindings: dict[str, _Binding]) -> MCPRuntime:
    return MCPRuntime(bindings=bindings, schemas=[], _stack=AsyncExitStack())


class TestMCPGatewayNode:
    async def test_unknown_tool_silent_drop(self):
        """A request for an unregistered tool must not publish anything."""
        from agentbus.bus import MessageBus
        from agentbus.message import Message
        from agentbus.node import Node
        from agentbus.schemas.common import ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        bus = MessageBus(socket_path=None)
        bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
        bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

        gateway = MCPGatewayNode(_make_runtime({}))
        bus.register_node(gateway)

        captured: list[Message] = []

        class Collector(Node):
            name = "collector"
            subscriptions = ["/tools/result"]
            publications: list[str] = []

            async def on_message(self, msg: Message) -> None:
                captured.append(msg)

        bus.register_node(Collector())
        bus.publish("/tools/request", ToolRequest(tool="some_builtin", params={}))

        await bus.spin(timeout=0.3)
        assert captured == []

    async def test_routes_to_matching_binding(self):
        from agentbus.bus import MessageBus
        from agentbus.message import Message
        from agentbus.node import Node
        from agentbus.schemas.common import ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        bus = MessageBus(socket_path=None)
        bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
        bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

        session = _FakeSession(
            {"read_file": _FakeCallResult(content=[_FakeTextBlock("file contents")])}
        )
        bindings = {
            mcp_tool_name("fs", "read_file"): _Binding(
                server="fs", tool_name="read_file", session=session
            ),
        }
        gateway = MCPGatewayNode(_make_runtime(bindings))
        bus.register_node(gateway)

        captured: list[Message] = []

        class Collector(Node):
            name = "collector"
            subscriptions = ["/tools/result"]
            publications: list[str] = []

            async def on_message(self, msg: Message) -> None:
                captured.append(msg)

        bus.register_node(Collector())
        bus.publish(
            "/tools/request",
            ToolRequest(tool="mcp__fs__read_file", params={"path": "/etc/hosts"}),
        )
        await bus.spin(until=lambda: bool(captured), timeout=5.0)

        assert captured
        payload: BusToolResult = captured[0].payload
        assert payload.output == "file contents"
        assert payload.error is None
        assert session.calls == [("read_file", {"path": "/etc/hosts"})]

    async def test_exception_from_session_becomes_error_result(self):
        from agentbus.bus import MessageBus
        from agentbus.message import Message
        from agentbus.node import Node
        from agentbus.schemas.common import ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        bus = MessageBus(socket_path=None)
        bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
        bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

        session = _FakeSession()
        session.fail_with = RuntimeError("boom")
        bindings = {
            mcp_tool_name("fs", "broken"): _Binding(
                server="fs", tool_name="broken", session=session
            ),
        }
        gateway = MCPGatewayNode(_make_runtime(bindings))
        bus.register_node(gateway)

        captured: list[Message] = []

        class Collector(Node):
            name = "collector"
            subscriptions = ["/tools/result"]
            publications: list[str] = []

            async def on_message(self, msg: Message) -> None:
                captured.append(msg)

        bus.register_node(Collector())
        bus.publish("/tools/request", ToolRequest(tool="mcp__fs__broken", params={}))
        await bus.spin(until=lambda: bool(captured), timeout=5.0)

        assert captured
        payload: BusToolResult = captured[0].payload
        assert payload.output is None
        assert "boom" in (payload.error or "")

    async def test_mcp_is_error_flag_becomes_error(self):
        from agentbus.bus import MessageBus
        from agentbus.message import Message
        from agentbus.node import Node
        from agentbus.schemas.common import ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        bus = MessageBus(socket_path=None)
        bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
        bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

        session = _FakeSession(
            {
                "flaky": _FakeCallResult(
                    content=[_FakeTextBlock("tool reported failure")], isError=True
                )
            }
        )
        bindings = {
            mcp_tool_name("srv", "flaky"): _Binding(
                server="srv", tool_name="flaky", session=session
            ),
        }
        gateway = MCPGatewayNode(_make_runtime(bindings))
        bus.register_node(gateway)

        captured: list[Message] = []

        class Collector(Node):
            name = "collector"
            subscriptions = ["/tools/result"]
            publications: list[str] = []

            async def on_message(self, msg: Message) -> None:
                captured.append(msg)

        bus.register_node(Collector())
        bus.publish("/tools/request", ToolRequest(tool="mcp__srv__flaky", params={}))
        await bus.spin(until=lambda: bool(captured), timeout=5.0)

        payload: BusToolResult = captured[0].payload
        assert payload.output is None
        assert payload.error == "tool reported failure"


# ── Coexistence with ChatToolNode ────────────────────────────────────────────


class TestCoexistence:
    async def test_chat_tool_node_drops_unknown_tools_for_mcp(self):
        """ChatToolNode must silently drop non-builtins so MCP can reply."""
        from agentbus.bus import MessageBus
        from agentbus.chat._tools import ChatToolNode
        from agentbus.message import Message
        from agentbus.node import Node
        from agentbus.schemas.common import ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        bus = MessageBus(socket_path=None)
        bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
        bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

        bus.register_node(ChatToolNode(["bash", "file_read"]))

        session = _FakeSession({"echo": _FakeCallResult(content=[_FakeTextBlock("mcp-reply")])})
        bindings = {
            mcp_tool_name("srv", "echo"): _Binding(server="srv", tool_name="echo", session=session),
        }
        bus.register_node(MCPGatewayNode(_make_runtime(bindings)))

        captured: list[Message] = []

        class Collector(Node):
            name = "collector"
            subscriptions = ["/tools/result"]
            publications: list[str] = []

            async def on_message(self, msg: Message) -> None:
                captured.append(msg)

        bus.register_node(Collector())
        bus.publish("/tools/request", ToolRequest(tool="mcp__srv__echo", params={}))
        await bus.spin(until=lambda: bool(captured), timeout=5.0)

        # Only one result — from MCP, not a "tool not enabled" from ChatToolNode.
        assert len(captured) == 1
        payload: BusToolResult = captured[0].payload
        assert payload.output == "mcp-reply"
        assert payload.error is None


# ── Config loading via ChatConfig ────────────────────────────────────────────


class TestChatConfigMCP:
    def test_mcp_servers_default_empty(self):
        from agentbus.chat._config import ChatConfig

        cfg = ChatConfig()
        assert cfg.mcp_servers == []

    def test_mcp_servers_loaded_from_yaml(self, tmp_path):
        import yaml

        from agentbus.chat._config import load_config

        path = tmp_path / "agentbus.yaml"
        path.write_text(
            yaml.dump(
                {
                    "provider": "anthropic",
                    "model": "claude-haiku",
                    "mcp_servers": [{"name": "fs", "command": "npx", "args": ["-y", "@pkg/fs"]}],
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert len(cfg.mcp_servers) == 1
        assert cfg.mcp_servers[0].name == "fs"
        assert cfg.mcp_servers[0].args == ["-y", "@pkg/fs"]

    def test_malformed_mcp_servers_raises(self, tmp_path):
        import yaml

        from agentbus.chat._config import load_config

        path = tmp_path / "agentbus.yaml"
        path.write_text(
            yaml.dump({"provider": "anthropic", "mcp_servers": [{"name": "x"}]}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            load_config(path)


# ── Require-mcp fail fast ────────────────────────────────────────────────────


class TestRequireSDK:
    def test_open_mcp_runtime_fail_fast_without_sdk(self, monkeypatch):
        """If the mcp package is unavailable, we raise SystemExit with install hint."""
        import builtins

        import agentbus.mcp as mcp_mod

        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == "mcp":
                raise ModuleNotFoundError("No module named 'mcp'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        with pytest.raises(SystemExit, match="not installed"):
            mcp_mod._require_mcp_sdk()
