"""Tests for agentbus.swarm — hub-and-spoke multi-agent orchestration."""

from __future__ import annotations

import asyncio

import pytest

from agentbus.bus import MessageBus
from agentbus.chat._config import ChatConfig
from agentbus.harness.providers import Chunk
from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.common import (
    InboundChat,
    OutboundChat,
    ToolRequest,
)
from agentbus.schemas.common import ToolResult as BusToolResult
from agentbus.swarm import (
    DISPATCH_TOOL_NAME,
    SubAgentSpec,
    SwarmAgentNode,
    SwarmCoordinatorNode,
    build_dispatch_tool_schema,
    register_swarm,
)
from agentbus.topic import Topic

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Always emits a fixed text response, no tool calls."""

    def __init__(self, text: str = "ok"):
        self._text = text
        self.context_window = 128_000

    async def complete(self, messages, tools, **kwargs):
        text = self._text

        async def _stream():
            yield Chunk(text=text)

        return _stream()

    def count_tokens(self, messages):
        return 0


class _FakeProviderWithToolCall:
    """First call: emit a tool_call. Second call (after tool_result): emit text."""

    def __init__(self, tool_name: str, tool_args: dict, text_after: str = "done"):
        self._tool_name = tool_name
        self._tool_args = tool_args
        self._text_after = text_after
        self._calls = 0
        self.context_window = 128_000

    async def complete(self, messages, tools, **kwargs):
        self._calls += 1
        n = self._calls
        if n == 1:
            tool_name = self._tool_name
            tool_args = self._tool_args

            async def _stream_call():
                yield Chunk(
                    tool_call_id="call-1",
                    tool_name=tool_name,
                    tool_arguments=tool_args,
                )

            return _stream_call()
        else:
            text = self._text_after

            async def _stream_text():
                yield Chunk(text=text)

            return _stream_text()

    def count_tokens(self, messages):
        return 0


class _DispatchDriver(Node):
    """Test helper — publishes a dispatch_subagent tool request via bus.request.

    Stores the reply (or exception) on the instance so tests can assert on it
    after the spin completes.
    """

    name = "dispatch-driver"
    subscriptions: list[str] = []
    publications = ["/tools/request"]

    def __init__(self, agent: str, task: str, tool: str = DISPATCH_TOOL_NAME):
        self._agent = agent
        self._task = task
        self._tool = tool
        self.result: BusToolResult | None = None
        self.exception: BaseException | None = None
        self.done = asyncio.Event()

    async def on_init(self, bus) -> None:
        async def _go():
            try:
                reply: Message = await bus.request(
                    "/tools/request",
                    ToolRequest(tool=self._tool, params={"agent": self._agent, "task": self._task}),
                    reply_on="/tools/result",
                    timeout=5.0,
                )
                self.result = reply.payload
            except BaseException as exc:
                self.exception = exc
            finally:
                self.done.set()

        asyncio.create_task(_go())


def _base_bus_with_tools_topics() -> MessageBus:
    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[ToolRequest]("/tools/request", retention=20))
    bus.register_topic(Topic[BusToolResult]("/tools/result", retention=20))
    return bus


# ---------------------------------------------------------------------------
# Pure function / validation tests
# ---------------------------------------------------------------------------


class TestToolSchema:
    def test_build_schema_lists_agents(self):
        specs = [
            SubAgentSpec(name="researcher", description="finds things", system_prompt="…"),
            SubAgentSpec(name="writer", description="writes things", system_prompt="…"),
        ]
        schema = build_dispatch_tool_schema(specs)
        assert schema.name == DISPATCH_TOOL_NAME
        assert "researcher: finds things" in schema.description
        assert "writer: writes things" in schema.description
        assert schema.input_schema["properties"]["agent"]["enum"] == ["researcher", "writer"]
        assert schema.input_schema["required"] == ["agent", "task"]


class TestRegisterSwarmValidation:
    def test_empty_specs_rejected(self):
        bus = _base_bus_with_tools_topics()
        with pytest.raises(ValueError, match="at least one"):
            register_swarm(bus, [], ChatConfig(provider="anthropic", model="fake"))

    def test_duplicate_names_rejected(self):
        bus = _base_bus_with_tools_topics()
        specs = [
            SubAgentSpec(name="a", description="x", system_prompt="p"),
            SubAgentSpec(name="a", description="y", system_prompt="q"),
        ]
        with pytest.raises(ValueError, match="duplicate"):
            register_swarm(bus, specs, ChatConfig(provider="anthropic", model="fake"))


class TestRegisterSwarmWiring:
    def test_registers_topics_and_nodes(self):
        bus = _base_bus_with_tools_topics()
        specs = [SubAgentSpec(name="r", description="d", system_prompt="p")]
        schema = register_swarm(
            bus,
            specs,
            ChatConfig(provider="anthropic", model="fake"),
            provider=_FakeProvider(),
        )
        assert schema.name == DISPATCH_TOOL_NAME
        assert "/swarm/r/inbound" in bus._topics
        assert "/swarm/r/outbound" in bus._topics
        assert "swarm-r" in bus._nodes
        assert "swarm-coordinator" in bus._nodes


# ---------------------------------------------------------------------------
# End-to-end dispatch tests
# ---------------------------------------------------------------------------


class TestDispatchFlow:
    async def test_dispatch_routes_to_sub_agent(self):
        """Coordinator dispatches to the sub-agent and returns its response."""
        bus = _base_bus_with_tools_topics()
        specs = [SubAgentSpec(name="echo", description="echoes", system_prompt="…")]
        register_swarm(
            bus,
            specs,
            ChatConfig(provider="anthropic", model="fake"),
            provider=_FakeProvider(text="hello from echo"),
        )
        driver = _DispatchDriver(agent="echo", task="say hi")
        bus.register_node(driver)

        await bus.spin(until=lambda: driver.done.is_set(), timeout=5.0)

        assert driver.exception is None
        assert driver.result is not None
        assert driver.result.error is None
        assert driver.result.output == "hello from echo"

    async def test_unknown_agent_returns_error(self):
        bus = _base_bus_with_tools_topics()
        specs = [SubAgentSpec(name="real", description="d", system_prompt="p")]
        register_swarm(
            bus,
            specs,
            ChatConfig(provider="anthropic", model="fake"),
            provider=_FakeProvider(),
        )
        driver = _DispatchDriver(agent="ghost", task="anything")
        bus.register_node(driver)

        await bus.spin(until=lambda: driver.done.is_set(), timeout=5.0)

        assert driver.exception is None
        assert driver.result.error is not None
        assert "unknown sub-agent" in driver.result.error
        assert "ghost" in driver.result.error

    async def test_empty_task_returns_error(self):
        bus = _base_bus_with_tools_topics()
        specs = [SubAgentSpec(name="real", description="d", system_prompt="p")]
        register_swarm(
            bus,
            specs,
            ChatConfig(provider="anthropic", model="fake"),
            provider=_FakeProvider(),
        )
        driver = _DispatchDriver(agent="real", task="   ")
        bus.register_node(driver)

        await bus.spin(until=lambda: driver.done.is_set(), timeout=5.0)

        assert driver.result.error == "'task' must be a non-empty string"

    async def test_non_dispatch_tool_silently_dropped(self):
        """Coordinator must not reply to tool requests it doesn't own.

        Publishes a non-dispatch request directly and captures every message
        on /tools/result — the coordinator should emit nothing.
        """
        bus = _base_bus_with_tools_topics()
        specs = [SubAgentSpec(name="a", description="d", system_prompt="p")]
        register_swarm(
            bus,
            specs,
            ChatConfig(provider="anthropic", model="fake"),
            provider=_FakeProvider(),
        )

        results_seen: list[Message] = []

        class ResultCapture(Node):
            name = "result-cap"
            subscriptions = ["/tools/result"]
            publications: list[str] = []

            async def on_message(self, msg: Message) -> None:
                results_seen.append(msg)

        bus.register_node(ResultCapture())

        async def seed():
            await asyncio.sleep(0.05)
            bus.publish(
                "/tools/request",
                ToolRequest(tool="something_else", params={"x": 1}),
                correlation_id="cid-drop",
            )

        asyncio.create_task(seed())
        await bus.spin(timeout=0.5, max_messages=5)

        assert results_seen == []


class TestSubAgentToolBridge:
    """Sub-agents route tool calls through the existing /tools/request bridge."""

    async def test_sub_agent_tool_call_flows_through_bus(self):
        bus = _base_bus_with_tools_topics()
        specs = [
            SubAgentSpec(
                name="worker",
                description="uses tools",
                system_prompt="…",
                tools=["bash"],  # telling the LLM about bash; handler lives in a fake node
            )
        ]
        # Sub-agent emits a tool_call first, then text after receiving tool_result.
        register_swarm(
            bus,
            specs,
            ChatConfig(provider="anthropic", model="fake"),
            provider=_FakeProviderWithToolCall(
                tool_name="bash", tool_args={"command": "true"}, text_after="finished"
            ),
        )

        # Fake tool handler: always succeeds with fixed output.
        class FakeToolNode(Node):
            name = "fake-tools"
            subscriptions = ["/tools/request"]
            publications = ["/tools/result"]

            def __init__(self):
                self.called_with: list[ToolRequest] = []
                self._bus = None

            async def on_init(self, bus):
                self._bus = bus

            async def on_message(self, msg: Message) -> None:
                req: ToolRequest = msg.payload
                if req.tool != "bash":
                    return
                self.called_with.append(req)
                await self._bus.publish(
                    "/tools/result",
                    BusToolResult(tool_call_id=msg.id, output="fake bash output", error=None),
                    correlation_id=msg.correlation_id,
                )

        tools_node = FakeToolNode()
        bus.register_node(tools_node)

        driver = _DispatchDriver(agent="worker", task="run something")
        bus.register_node(driver)

        await bus.spin(until=lambda: driver.done.is_set(), timeout=5.0)

        assert driver.exception is None
        assert driver.result is not None
        assert driver.result.error is None
        assert driver.result.output == "finished"
        assert len(tools_node.called_with) == 1
        assert tools_node.called_with[0].tool == "bash"


# ---------------------------------------------------------------------------
# Direct SwarmAgentNode tests
# ---------------------------------------------------------------------------


class TestSwarmAgentNode:
    def test_node_attrs(self):
        spec = SubAgentSpec(name="r", description="d", system_prompt="p")
        node = SwarmAgentNode(
            spec, ChatConfig(provider="anthropic", model="fake"), provider=_FakeProvider()
        )
        assert node.name == "swarm-r"
        assert node.subscriptions == ["/swarm/r/inbound"]
        assert "/swarm/r/outbound" in node.publications
        assert "/tools/request" in node.publications
        assert node.concurrency_mode == "serial"


class TestCoordinatorAttrs:
    def test_coordinator_declares_all_subagent_inboxes(self):
        specs = [
            SubAgentSpec(name="a", description="d", system_prompt="p"),
            SubAgentSpec(name="b", description="d", system_prompt="p"),
        ]
        node = SwarmCoordinatorNode(specs)
        assert node.subscriptions == ["/tools/request"]
        assert "/tools/result" in node.publications
        assert "/swarm/a/inbound" in node.publications
        assert "/swarm/b/inbound" in node.publications


# ---------------------------------------------------------------------------
# Correlation-ID preservation
# ---------------------------------------------------------------------------


class TestCorrelationID:
    async def test_sub_agent_echoes_correlation_id(self):
        """SwarmAgentNode must preserve msg.correlation_id on outbound publish.

        This is what unblocks the coordinator's bus.request future.
        """
        bus = _base_bus_with_tools_topics()
        specs = [SubAgentSpec(name="r", description="d", system_prompt="p")]
        register_swarm(
            bus,
            specs,
            ChatConfig(provider="anthropic", model="fake"),
            provider=_FakeProvider(text="reply"),
        )

        # Capture all messages on /swarm/r/outbound
        captured: list[Message] = []

        class Capture(Node):
            name = "cap"
            subscriptions = ["/swarm/r/outbound"]
            publications: list[str] = []

            async def on_message(self, msg: Message) -> None:
                captured.append(msg)

        bus.register_node(Capture())

        # Publish directly with a known correlation_id
        cid = "test-cid-123"

        async def seed():
            await asyncio.sleep(0.05)
            bus.publish(
                "/swarm/r/inbound",
                InboundChat(channel="test", sender="tester", text="hi"),
                correlation_id=cid,
            )

        asyncio.create_task(seed())
        await bus.spin(until=lambda: len(captured) > 0, timeout=5.0)

        assert len(captured) == 1
        assert captured[0].correlation_id == cid
        assert isinstance(captured[0].payload, OutboundChat)
        assert captured[0].payload.text == "reply"
