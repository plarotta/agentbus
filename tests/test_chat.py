"""Tests for agentbus.chat — Phase 1 (headless) + slash commands."""

from __future__ import annotations

from agentbus.chat._commands import handle_command
from agentbus.chat._config import ChatConfig, load_config
from agentbus.chat._runner import ChatSession
from agentbus.chat._sandbox import SandboxConfig, SubprocessSandbox
from agentbus.chat._tools import (
    TOOL_SCHEMAS,
    ChatToolNode,
    _run_bash,
    _run_code_exec,
    _run_file_read,
    _run_file_write,
)
from agentbus.harness.providers import Chunk, ToolSchema
from agentbus.schemas.common import InboundChat, OutboundChat
from agentbus.schemas.harness import PlannerStatus

_SANDBOX = SubprocessSandbox(SandboxConfig())

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(config: ChatConfig | None = None) -> ChatSession:
    cfg = config or ChatConfig(
        provider="anthropic", model="test-model", tools=["bash", "file_read"]
    )
    return ChatSession(cfg, headless=True, verbose=False)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestChatConfig:
    def test_defaults(self):
        c = ChatConfig()
        assert c.provider == "ollama"
        assert "bash" in c.tools
        assert c.memory is False

    def test_save_and_load_json(self, tmp_path):
        p = tmp_path / "config.yaml"
        c = ChatConfig(provider="anthropic", model="claude-test", tools=["bash"], memory=False)
        # Force JSON (no yaml module guaranteed in test env)
        import json as _json

        p.write_text(
            _json.dumps(
                {
                    "provider": c.provider,
                    "model": c.model,
                    "tools": c.tools,
                    "memory": c.memory,
                }
            )
        )
        loaded = load_config(p)
        assert loaded.provider == "anthropic"
        assert loaded.model == "claude-test"
        assert loaded.tools == ["bash"]


# ---------------------------------------------------------------------------
# Tool handler tests
# ---------------------------------------------------------------------------


class TestToolHandlers:
    async def test_bash_echo(self):
        result = await _run_bash({"command": "echo hello"}, _SANDBOX)
        assert result == "hello"

    async def test_bash_stderr(self):
        result = await _run_bash({"command": "echo out; echo err >&2"}, _SANDBOX)
        assert "out" in result
        assert "err" in result

    async def test_bash_timeout(self):
        result = await _run_bash({"command": "sleep 10", "timeout": 0.1}, _SANDBOX)
        assert "timed out" in result

    async def test_file_write_and_read(self, tmp_path):
        p = str(tmp_path / "sub" / "file.txt")
        write_result = await _run_file_write({"path": p, "content": "hello world"})
        assert "Wrote" in write_result
        read_result = await _run_file_read({"path": p})
        assert read_result == "hello world"

    async def test_file_read_missing(self):
        result = await _run_file_read({"path": "/no/such/file.xyz"})
        assert "Error" in result

    async def test_code_exec(self):
        result = await _run_code_exec({"code": "print(2 + 2)"}, _SANDBOX)
        assert result == "4"

    def test_tool_schemas_present(self):
        for name in ("bash", "file_read", "file_write", "code_exec"):
            assert name in TOOL_SCHEMAS
            schema = TOOL_SCHEMAS[name]
            assert isinstance(schema, ToolSchema)
            assert schema.name == name


# ---------------------------------------------------------------------------
# ChatToolNode tests (with mock bus)
# ---------------------------------------------------------------------------


class TestChatToolNode:
    async def test_dispatches_bash(self, tmp_path):
        """ChatToolNode processes a ToolRequest and publishes the result."""
        from agentbus.bus import MessageBus
        from agentbus.message import Message
        from agentbus.schemas.common import ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        bus = MessageBus(socket_path=None)
        bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
        bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))
        bus.register_node(ChatToolNode(["bash"]))

        results: list[Message] = []

        class Collector:
            name = "collector"
            subscriptions = ["/tools/result"]
            publications: list = []

            async def on_message(self, msg):
                results.append(msg)

        from agentbus.node import Node

        class CollectorNode(Node):
            name = "collector"
            subscriptions = ["/tools/result"]
            publications: list[str] = []

            async def on_message(self, msg: Message) -> None:
                results.append(msg)

        bus.register_node(CollectorNode())

        # Publish a tool request and spin until result arrives
        bus.publish("/tools/request", ToolRequest(tool="bash", params={"command": "echo hi"}))
        await bus.spin(until=lambda: bool(results), timeout=5.0)

        assert results
        payload: BusToolResult = results[0].payload
        assert payload.output == "hi"

    async def test_disabled_tool_returns_error(self):
        """Requesting a disabled tool returns an error in BusToolResult."""
        from agentbus.bus import MessageBus
        from agentbus.message import Message
        from agentbus.node import Node
        from agentbus.schemas.common import ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        bus = MessageBus(socket_path=None)
        bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
        bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))
        # ChatToolNode with no enabled tools
        bus.register_node(ChatToolNode([]))

        results: list[Message] = []

        class CollectorNode(Node):
            name = "collector"
            subscriptions = ["/tools/result"]
            publications: list[str] = []

            async def on_message(self, msg: Message) -> None:
                results.append(msg)

        bus.register_node(CollectorNode())
        bus.publish("/tools/request", ToolRequest(tool="bash", params={"command": "echo x"}))
        await bus.spin(until=lambda: bool(results), timeout=5.0)

        assert results
        payload: BusToolResult = results[0].payload
        assert payload.error is not None
        assert "not enabled" in payload.error


# ---------------------------------------------------------------------------
# Bus setup tests
# ---------------------------------------------------------------------------


class TestChatSessionBusSetup:
    def test_all_topics_registered(self):
        session = _make_session()
        bus = session._build_bus()
        names = {t.name for t in bus.topics()}
        for expected in [
            "/inbound",
            "/outbound",
            "/tools/request",
            "/tools/result",
            "/planning/status",
        ]:
            assert expected in names, f"Missing topic: {expected}"

    def test_all_nodes_registered(self):
        session = _make_session()
        bus = session._build_bus()
        names = {n.name for n in bus.nodes()}
        assert "planner" in names
        assert "chat_tools" in names
        assert "chat_capture" in names

    def test_no_tools_no_tool_node(self):
        cfg = ChatConfig(provider="anthropic", model="test", tools=[])
        session = ChatSession(cfg, headless=True)
        bus = session._build_bus()
        names = {n.name for n in bus.nodes()}
        assert "chat_tools" not in names
        assert "planner" in names


# ---------------------------------------------------------------------------
# Slash command tests
# ---------------------------------------------------------------------------


class TestSlashCommands:
    def _bus_and_planner(self):
        cfg = ChatConfig(provider="anthropic", model="test", tools=["bash"])
        s = ChatSession(cfg, headless=True)
        bus = s._build_bus()
        return bus, s._planner, cfg

    async def test_help(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/help", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "Introspection" in result.output

    async def test_quit(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/quit", bus=bus, planner=planner, config=cfg)
        assert result.quit is True

    async def test_exit_alias(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/exit", bus=bus, planner=planner, config=cfg)
        assert result.quit is True

    async def test_topics(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/topics", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "/inbound" in result.output

    async def test_nodes(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/nodes", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "planner" in result.output

    async def test_session(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/session", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "Session ID" in result.output

    async def test_tools(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/tools", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "bash" in result.output

    async def test_provider(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/provider", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "anthropic" in result.output

    async def test_unknown_command(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/xyzzy", bus=bus, planner=planner, config=cfg)
        assert result.error is not None
        assert "Unknown command" in result.error

    async def test_graph(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/graph", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "graph TD" in result.output

    async def test_echo_no_retention(self):
        """Echo on a topic with no messages should say so."""
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/echo /inbound", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "No retained" in result.output

    async def test_history_empty(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/history", bus=bus, planner=planner, config=cfg)
        assert result.output is not None

    async def test_inspect_returns_toggle(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/inspect", bus=bus, planner=planner, config=cfg)
        assert result.inspect_toggle is not None

    async def test_inspect_with_pattern(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/inspect /tools/*", bus=bus, planner=planner, config=cfg)
        assert result.inspect_toggle == "/tools/*"

    async def test_breakers(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/breakers", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "planner" in result.output

    async def test_session_fork(self):
        bus, planner, cfg = self._bus_and_planner()
        # Add a turn so fork has something to work with
        from agentbus.schemas.harness import ConversationTurn

        planner.session.append(ConversationTurn(role="user", content="hello"))
        result = await handle_command("/session fork", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "Forked" in result.output

    async def test_trace_empty_log(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/trace", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "No messages" in result.output

    async def test_trace_no_correlation_ids(self):
        bus, planner, cfg = self._bus_and_planner()
        bus.publish("/inbound", InboundChat(channel="cli", sender="u", text="hi"))
        result = await handle_command("/trace", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "No correlation IDs" in result.output

    async def test_trace_follows_correlation_id(self):
        bus, planner, cfg = self._bus_and_planner()
        cid = "abcd1234-corr"
        # Publish a pair of correlated messages plus one uncorrelated.
        bus.publish(
            "/inbound",
            InboundChat(channel="cli", sender="u", text="first"),
            correlation_id=cid,
        )
        bus.publish("/outbound", OutboundChat(text="reply", reply_to="u"), correlation_id=cid)
        bus.publish("/inbound", InboundChat(channel="cli", sender="u", text="noise"))

        result = await handle_command("/trace", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert cid[:8] in result.output
        assert "2 message(s)" in result.output
        assert "/inbound" in result.output
        assert "/outbound" in result.output
        # Noise message (no correlation_id) must not appear.
        assert "noise" not in result.output

    async def test_trace_by_cid_prefix(self):
        bus, planner, cfg = self._bus_and_planner()
        cid = "deadbeef-corr"
        bus.publish(
            "/inbound",
            InboundChat(channel="cli", sender="u", text="x"),
            correlation_id=cid,
        )
        result = await handle_command("/trace deadbeef", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "deadbeef" in result.output

    async def test_trace_by_topic(self):
        bus, planner, cfg = self._bus_and_planner()
        cid = "feedface-corr"
        bus.publish(
            "/outbound",
            OutboundChat(text="hi", reply_to="u"),
            correlation_id=cid,
        )
        result = await handle_command("/trace /outbound", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert cid[:8] in result.output

    async def test_trace_by_topic_no_match(self):
        bus, planner, cfg = self._bus_and_planner()
        # Publish something else so the log isn't empty.
        bus.publish(
            "/inbound",
            InboundChat(channel="cli", sender="u", text="x"),
            correlation_id="cid-x",
        )
        result = await handle_command("/trace /outbound", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "No correlated messages" in result.output

    async def test_usage_empty(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/usage", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "No turns" in result.output

    async def test_usage_aggregates_by_role(self):
        from agentbus.schemas.harness import ConversationTurn

        bus, planner, cfg = self._bus_and_planner()
        planner.session.append(ConversationTurn(role="user", content="a", token_count=5))
        planner.session.append(ConversationTurn(role="assistant", content="b", token_count=7))
        planner.session.append(ConversationTurn(role="user", content="c", token_count=3))

        result = await handle_command("/usage", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "Total tokens: 15" in result.output
        assert "anthropic" in result.output
        assert "user" in result.output
        assert "assistant" in result.output

    async def test_help_lists_new_commands(self):
        bus, planner, cfg = self._bus_and_planner()
        result = await handle_command("/help", bus=bus, planner=planner, config=cfg)
        assert result.output is not None
        assert "/trace" in result.output
        assert "/usage" in result.output


# ---------------------------------------------------------------------------
# End-to-end integration test with fake provider
# ---------------------------------------------------------------------------


class TestChatSessionIntegration:
    """Test the full message flow with a fake provider injected directly."""

    async def test_full_loop_with_fake_provider(self):
        """Publish InboundChat → response arrives in response_queue."""
        from agentbus.bus import MessageBus
        from agentbus.chat._planner import ChatPlannerNode
        from agentbus.schemas.common import ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        # Fake provider: always responds "pong"
        class FakeProvider:
            context_window = 128_000

            async def complete(self, messages, tools, **kwargs):
                async def _stream():
                    yield Chunk(text="pong")

                return _stream()

            def count_tokens(self, messages):
                return 0

        # Build the bus manually with an injected fake provider
        bus = MessageBus(socket_path=None)
        bus.register_topic(Topic[InboundChat]("/inbound", retention=20))
        bus.register_topic(Topic[OutboundChat]("/outbound", retention=20))
        bus.register_topic(Topic[ToolRequest]("/tools/request", retention=10))
        bus.register_topic(Topic[BusToolResult]("/tools/result", retention=10))
        bus.register_topic(Topic[PlannerStatus]("/planning/status", retention=10))

        import asyncio as _asyncio

        response_queue: _asyncio.Queue[OutboundChat] = _asyncio.Queue()
        status_queue: _asyncio.Queue[PlannerStatus] = _asyncio.Queue()

        from agentbus.chat._runner import _ChatCaptureNode

        planner = ChatPlannerNode(
            ChatConfig(provider="anthropic", model="fake", tools=[]),
            provider=FakeProvider(),  # injected — skips _make_provider import check
        )
        bus.register_node(planner)
        bus.register_node(_ChatCaptureNode(response_queue, status_queue))

        # Publish a user message after a short delay (let on_init run first)
        async def send_after_init():
            await _asyncio.sleep(0.05)
            bus.publish("/inbound", InboundChat(channel="test", sender="tester", text="ping"))

        _asyncio.create_task(send_after_init())
        await bus.spin(until=lambda: not response_queue.empty(), timeout=10.0)

        assert not response_queue.empty()
        response: OutboundChat = response_queue.get_nowait()
        assert response.text == "pong"
