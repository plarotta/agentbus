"""Phase 3b tests: introspection APIs and Unix socket server."""

import asyncio
import json
import os

import pytest

from agentbus.bus import MessageBus
from agentbus.errors import RequestTimeoutError
from agentbus.node import Node, NodeState
from agentbus.schemas.common import InboundChat, OutboundChat
from agentbus.topic import Topic

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_bus(socket_path: str | None = None) -> MessageBus:
    return MessageBus(socket_path=socket_path)


def inbound(text: str = "hi") -> InboundChat:
    return InboundChat(channel="cli", sender="user", text=text)


class ListenerNode(Node):
    name = "listener"
    subscriptions = ["/inbound"]
    publications = ["/outbound"]

    async def on_message(self, msg):
        pass


class PublisherNode(Node):
    name = "publisher"
    subscriptions = []
    publications = ["/inbound"]

    async def on_message(self, msg):
        pass


def make_wired_bus(socket_path: str | None = None) -> tuple[MessageBus, ListenerNode]:
    bus = make_bus(socket_path=socket_path)
    bus.register_topic(Topic[InboundChat]("/inbound", retention=20))
    bus.register_topic(Topic[OutboundChat]("/outbound"))
    node = ListenerNode()
    bus.register_node(node)
    return bus, node


# ── topics() ─────────────────────────────────────────────────────────────────


def test_topics_includes_system_and_user():
    bus, _ = make_wired_bus()
    names = {t.name for t in bus.topics()}
    assert "/inbound" in names
    assert "/outbound" in names
    assert "/system/lifecycle" in names
    assert "/system/heartbeat" in names


def test_topics_subscriber_count():
    bus, _ = make_wired_bus()
    by_name = {t.name: t for t in bus.topics()}
    assert by_name["/inbound"].subscriber_count == 1
    assert by_name["/outbound"].subscriber_count == 0


def test_topics_schema_name():
    bus, _ = make_wired_bus()
    by_name = {t.name: t for t in bus.topics()}
    assert by_name["/inbound"].schema_name == "InboundChat"


def test_topics_retention_reflected():
    bus, _ = make_wired_bus()
    by_name = {t.name: t for t in bus.topics()}
    assert by_name["/inbound"].retention == 20
    assert by_name["/outbound"].retention == 0


# ── nodes() ──────────────────────────────────────────────────────────────────


def test_nodes_returns_registered_nodes():
    bus, _ = make_wired_bus()
    names = {n.name for n in bus.nodes()}
    assert "listener" in names


def test_nodes_initial_state_is_created():
    bus, _ = make_wired_bus()
    by_name = {n.name: n for n in bus.nodes()}
    assert by_name["listener"].state == NodeState.CREATED.value


def test_nodes_subscriptions_and_publications():
    bus, _ = make_wired_bus()
    by_name = {n.name: n for n in bus.nodes()}
    assert "/inbound" in by_name["listener"].subscriptions
    assert "/outbound" in by_name["listener"].publications


# ── graph() ───────────────────────────────────────────────────────────────────


def test_graph_contains_all_nodes_and_topics():
    bus, _ = make_wired_bus()
    g = bus.graph()
    node_names = {n.name for n in g.nodes}
    topic_names = {t.name for t in g.topics}
    assert "listener" in node_names
    assert "/inbound" in topic_names


def test_graph_edges_match_declarations():
    bus, _ = make_wired_bus()
    g = bus.graph()
    edge_tuples = {(e.node, e.topic, e.direction) for e in g.edges}
    assert ("listener", "/inbound", "sub") in edge_tuples
    assert ("listener", "/outbound", "pub") in edge_tuples


# ── history() ────────────────────────────────────────────────────────────────


def test_history_with_retention():
    bus, _ = make_wired_bus()
    bus.publish("/inbound", inbound("first"))
    bus.publish("/inbound", inbound("second"))
    msgs = bus.history("/inbound")
    assert len(msgs) == 2
    assert msgs[0].payload.text == "first"
    assert msgs[1].payload.text == "second"


def test_history_last_n():
    bus, _ = make_wired_bus()
    for i in range(5):
        bus.publish("/inbound", inbound(str(i)))
    msgs = bus.history("/inbound", n=3)
    assert len(msgs) == 3
    assert msgs[-1].payload.text == "4"


def test_history_no_retention_returns_empty():
    bus, _ = make_wired_bus()
    bus.publish("/outbound", OutboundChat(text="hi"))
    msgs = bus.history("/outbound")
    assert msgs == []


def test_history_unknown_topic_returns_empty():
    bus, _ = make_wired_bus()
    assert bus.history("/nonexistent") == []


# ── wait_for() ────────────────────────────────────────────────────────────────


async def test_wait_for_resolves_on_matching_message():
    bus, _ = make_wired_bus()

    async def _publish():
        await asyncio.sleep(0.05)
        bus.publish("/inbound", inbound("target"))

    asyncio.create_task(_publish())
    msg = await bus.wait_for("/inbound", lambda m: m.payload.text == "target", timeout=2.0)
    assert msg.payload.text == "target"


async def test_wait_for_skips_non_matching():
    bus, _ = make_wired_bus()

    async def _publish():
        await asyncio.sleep(0.03)
        bus.publish("/inbound", inbound("skip"))
        await asyncio.sleep(0.03)
        bus.publish("/inbound", inbound("match"))

    asyncio.create_task(_publish())
    msg = await bus.wait_for("/inbound", lambda m: m.payload.text == "match", timeout=2.0)
    assert msg.payload.text == "match"


async def test_wait_for_timeout_raises():
    bus, _ = make_wired_bus()
    with pytest.raises(RequestTimeoutError):
        await bus.wait_for("/inbound", lambda m: False, timeout=0.1)


async def test_wait_for_unknown_topic_raises():
    bus, _ = make_wired_bus()
    with pytest.raises(RequestTimeoutError):
        await bus.wait_for("/nonexistent", lambda m: True, timeout=0.1)


async def test_wait_for_does_not_consume_real_subscriber_messages():
    """Messages observed via wait_for still reach real subscribers."""
    bus, _ = make_wired_bus()
    bus.publish("/inbound", inbound("before"))

    async def _publish():
        await asyncio.sleep(0.03)
        bus.publish("/inbound", inbound("observed"))

    asyncio.create_task(_publish())
    msg = await bus.wait_for("/inbound", lambda m: m.payload.text == "observed", timeout=2.0)
    assert msg.payload.text == "observed"
    # Real subscriber queue should have both messages
    handle = bus._nodes["listener"]
    assert handle.queue.qsize() == 2


# ── echo() ────────────────────────────────────────────────────────────────────


async def test_echo_yields_n_messages():
    bus, _ = make_wired_bus()

    async def _publish():
        await asyncio.sleep(0.03)
        for text in ("a", "b", "c"):
            bus.publish("/inbound", inbound(text))

    asyncio.create_task(_publish())
    results = []
    async for msg in bus.echo("/inbound", n=3):
        results.append(msg.payload.text)

    assert results == ["a", "b", "c"]


async def test_echo_filter():
    bus, _ = make_wired_bus()

    async def _publish():
        await asyncio.sleep(0.03)
        bus.publish("/inbound", inbound("skip"))
        bus.publish("/inbound", inbound("keep"))

    asyncio.create_task(_publish())
    results = []
    async for msg in bus.echo("/inbound", n=1, filter=lambda m: m.payload.text == "keep"):
        results.append(msg.payload.text)

    assert results == ["keep"]


async def test_echo_unknown_topic_yields_nothing():
    bus, _ = make_wired_bus()
    results = []
    async for msg in bus.echo("/nonexistent", n=1):
        results.append(msg)
    assert results == []


async def test_echo_removes_temp_subscriber_on_exit():
    bus, _ = make_wired_bus()
    topic = bus._topics["/inbound"]
    subs_before = set(topic._subscribers.keys())

    async def _publish():
        await asyncio.sleep(0.03)
        bus.publish("/inbound", inbound("x"))

    asyncio.create_task(_publish())
    async for _ in bus.echo("/inbound", n=1):
        pass

    subs_after = set(topic._subscribers.keys())
    assert subs_after == subs_before  # temp subscriber cleaned up


async def test_echo_removes_temp_subscriber_on_cancel():
    bus, _ = make_wired_bus()
    topic = bus._topics["/inbound"]
    subs_before = set(topic._subscribers.keys())

    async def _consume():
        async for _ in bus.echo("/inbound"):  # unlimited — will block
            pass

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    subs_after = set(topic._subscribers.keys())
    assert subs_after == subs_before


# ── Socket server (integration) ───────────────────────────────────────────────


async def test_socket_topics_command(short_tmp):
    socket_path = os.path.join(short_tmp, "agentbus.sock")
    bus, _ = make_wired_bus(socket_path=socket_path)

    spin_task = asyncio.create_task(bus.spin(timeout=2.0))
    # Wait briefly for the socket server to start accepting connections
    for _ in range(20):
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
    else:
        pytest.fail("Socket server did not start in time")

    writer.write(json.dumps({"cmd": "topics"}).encode() + b"\n")
    await writer.drain()
    response = await reader.readline()
    data = json.loads(response)
    writer.close()
    await writer.wait_closed()

    spin_task.cancel()
    await asyncio.gather(spin_task, return_exceptions=True)

    topic_names = [t["name"] for t in data]
    assert "/inbound" in topic_names
    assert "/system/lifecycle" in topic_names


async def test_socket_nodes_command(short_tmp):
    socket_path = os.path.join(short_tmp, "agentbus.sock")
    bus, _ = make_wired_bus(socket_path=socket_path)

    spin_task = asyncio.create_task(bus.spin(timeout=2.0))
    for _ in range(20):
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
    else:
        pytest.fail("Socket server did not start in time")

    writer.write(json.dumps({"cmd": "nodes"}).encode() + b"\n")
    await writer.drain()
    response = await reader.readline()
    data = json.loads(response)
    writer.close()
    await writer.wait_closed()

    spin_task.cancel()
    await asyncio.gather(spin_task, return_exceptions=True)

    node_names = [n["name"] for n in data]
    assert "listener" in node_names


async def test_socket_graph_command(short_tmp):
    socket_path = os.path.join(short_tmp, "agentbus.sock")
    bus, _ = make_wired_bus(socket_path=socket_path)

    spin_task = asyncio.create_task(bus.spin(timeout=2.0))
    for _ in range(20):
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
    else:
        pytest.fail("Socket server did not start in time")

    writer.write(json.dumps({"cmd": "graph"}).encode() + b"\n")
    await writer.drain()
    response = await reader.readline()
    data = json.loads(response)
    writer.close()
    await writer.wait_closed()

    spin_task.cancel()
    await asyncio.gather(spin_task, return_exceptions=True)

    assert "nodes" in data
    assert "topics" in data
    assert "edges" in data
    edge_tuples = {(e["node"], e["topic"], e["direction"]) for e in data["edges"]}
    assert ("listener", "/inbound", "sub") in edge_tuples


async def test_socket_history_command(short_tmp):
    socket_path = os.path.join(short_tmp, "agentbus.sock")
    bus, _ = make_wired_bus(socket_path=socket_path)
    bus.publish("/inbound", inbound("stored"))

    spin_task = asyncio.create_task(bus.spin(timeout=2.0))
    for _ in range(20):
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
    else:
        pytest.fail("Socket server did not start in time")

    writer.write(json.dumps({"cmd": "history", "topic": "/inbound", "n": 5}).encode() + b"\n")
    await writer.drain()
    response = await reader.readline()
    data = json.loads(response)
    writer.close()
    await writer.wait_closed()

    spin_task.cancel()
    await asyncio.gather(spin_task, return_exceptions=True)

    assert len(data) == 1
    assert data[0]["payload"]["text"] == "stored"


async def test_socket_unknown_command(short_tmp):
    socket_path = os.path.join(short_tmp, "agentbus.sock")
    bus, _ = make_wired_bus(socket_path=socket_path)

    spin_task = asyncio.create_task(bus.spin(timeout=2.0))
    for _ in range(20):
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
    else:
        pytest.fail("Socket server did not start in time")

    writer.write(json.dumps({"cmd": "bogus"}).encode() + b"\n")
    await writer.drain()
    response = await reader.readline()
    data = json.loads(response)
    writer.close()
    await writer.wait_closed()

    spin_task.cancel()
    await asyncio.gather(spin_task, return_exceptions=True)

    assert "error" in data


async def test_socket_node_info_command(short_tmp):
    socket_path = os.path.join(short_tmp, "agentbus.sock")
    bus, _ = make_wired_bus(socket_path=socket_path)

    spin_task = asyncio.create_task(bus.spin(timeout=2.0))
    for _ in range(20):
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
    else:
        pytest.fail("Socket server did not start in time")

    writer.write(json.dumps({"cmd": "node_info", "name": "listener"}).encode() + b"\n")
    await writer.drain()
    response = await reader.readline()
    data = json.loads(response)
    writer.close()
    await writer.wait_closed()

    spin_task.cancel()
    await asyncio.gather(spin_task, return_exceptions=True)

    assert data["name"] == "listener"
    assert "/inbound" in data["subscriptions"]


async def test_socket_cleans_up_file_after_shutdown(short_tmp):
    socket_path = os.path.join(short_tmp, "agentbus.sock")
    bus, _ = make_wired_bus(socket_path=socket_path)

    spin_task = asyncio.create_task(bus.spin(timeout=0.2))
    await asyncio.gather(spin_task, return_exceptions=True)

    assert not os.path.exists(socket_path)
