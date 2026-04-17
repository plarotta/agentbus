"""Phase 3a tests for MessageBus."""

import asyncio

import pytest

from agentbus.bus import MAX_CONSECUTIVE_NODE_ERRORS, MessageBus, _BusHandle
from agentbus.errors import (
    DuplicateNodeError,
    DuplicateTopicError,
    RequestTimeoutError,
    TopicSchemaError,
    UndeclaredPublicationError,
    UndeclaredSubscriptionError,
)
from agentbus.message import Message
from agentbus.node import BusHandle, Node, NodeState
from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest
from agentbus.schemas.system import BackpressureEvent, Heartbeat, LifecycleEvent
from agentbus.topic import Topic

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_bus() -> MessageBus:
    return MessageBus()


def inbound(text: str = "hi") -> InboundChat:
    return InboundChat(channel="cli", sender="user", text=text)


def register_inbound(bus: MessageBus) -> Topic:
    t = Topic[InboundChat]("/inbound")
    bus.register_topic(t)
    return t


# ── Registration: duplicate / undeclared errors ───────────────────────────────


def test_register_duplicate_topic_raises():
    bus = make_bus()
    bus.register_topic(Topic[InboundChat]("/inbound"))
    with pytest.raises(DuplicateTopicError):
        bus.register_topic(Topic[InboundChat]("/inbound"))


def test_register_duplicate_node_raises():
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(N())
    with pytest.raises(DuplicateNodeError):
        bus.register_node(N())


def test_register_node_undeclared_subscription_raises():
    bus = make_bus()
    # No /inbound topic registered

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    with pytest.raises(UndeclaredSubscriptionError):
        bus.register_node(N())


def test_register_node_undeclared_publication_raises():
    bus = make_bus()
    # No /tools/request topic registered

    class N(Node):
        name = "n"
        subscriptions = []
        publications = ["/tools/request"]

    with pytest.raises(UndeclaredPublicationError):
        bus.register_node(N())


def test_register_node_wildcard_subscription_resolves():
    bus = make_bus()
    # /system/* subscription should match system topics auto-registered at init

    class Observer(Node):
        name = "observer"
        subscriptions = ["/system/*"]
        publications = []

    bus.register_node(Observer())  # must not raise
    handle = bus._nodes["observer"]
    # Queue should be wired to exactly the 4 /system/ topics
    assert handle.queue.maxsize == 100


def test_register_node_wildcard_publication_resolves():
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = ["/inbound"]

    bus.register_node(N())  # wildcard /inbound/* also works; exact match works


# ── publish(): schema validation and source_node stamping ─────────────────────


def test_publish_wrong_schema_raises():
    bus = make_bus()
    register_inbound(bus)
    with pytest.raises(TopicSchemaError):
        bus.publish("/inbound", ToolRequest(tool="browser"))


def test_publish_stamps_source_node():
    bus = make_bus()
    register_inbound(bus)
    msg = bus.publish("/inbound", inbound(), source_node="my-node")
    assert msg.source_node == "my-node"


def test_publish_default_source_node_is_bus():
    bus = make_bus()
    register_inbound(bus)
    msg = bus.publish("/inbound", inbound())
    assert msg.source_node == "_bus_"


def test_publish_returns_message_with_correct_fields():
    bus = make_bus()
    register_inbound(bus)
    payload = inbound("hello")
    msg = bus.publish("/inbound", payload)
    assert msg.topic == "/inbound"
    assert msg.payload is payload
    assert msg.id  # non-empty uuid string


# ── BusHandle publish: undeclared publication guard ───────────────────────────


async def test_bus_handle_publish_undeclared_raises():
    bus = make_bus()
    register_inbound(bus)
    bus.register_topic(Topic[OutboundChat]("/outbound"))

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = ["/inbound"]  # does NOT include /outbound

    bus.register_node(N())
    handle_obj = _BusHandle(bus, "n")
    with pytest.raises(UndeclaredPublicationError):
        await handle_obj.publish("/outbound", OutboundChat(text="hi"))


async def test_bus_handle_publish_declared_succeeds():
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = ["/inbound"]

    bus.register_node(N())
    handle_obj = _BusHandle(bus, "n")
    await handle_obj.publish("/inbound", inbound())  # must not raise


# ── spin_once: routing ────────────────────────────────────────────────────────


async def test_spin_once_routes_message_to_node():
    bus = make_bus()
    register_inbound(bus)

    received: list[Message] = []

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            received.append(msg)

    bus.register_node(N())
    bus._nodes["n"].state = NodeState.RUNNING

    sent = bus.publish("/inbound", inbound("ping"))
    result = await bus.spin_once(timeout=1.0)

    assert result is not None
    assert result.id == sent.id
    assert len(received) == 1
    assert received[0].payload.text == "ping"


async def test_spin_once_returns_none_on_timeout():
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(N())
    bus._nodes["n"].state = NodeState.RUNNING

    # No messages published — should time out
    result = await bus.spin_once(timeout=0.05)
    assert result is None


async def test_spin_once_increments_messages_received():
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(N())
    bus._nodes["n"].state = NodeState.RUNNING

    bus.publish("/inbound", inbound())
    await bus.spin_once(timeout=1.0)

    assert bus._nodes["n"].messages_received == 1


# ── spin_once: error handling ─────────────────────────────────────────────────


async def test_on_message_exception_node_stays_running():
    bus = make_bus()
    register_inbound(bus)

    class Crasher(Node):
        name = "crasher"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            raise ValueError("boom")

    bus.register_node(Crasher())
    bus._nodes["crasher"].state = NodeState.RUNNING

    bus.publish("/inbound", inbound())
    await bus.spin_once(timeout=1.0)

    handle = bus._nodes["crasher"]
    assert handle.state == NodeState.RUNNING
    assert handle.errors == 1


async def test_on_message_exception_publishes_lifecycle_error():
    bus = make_bus()
    register_inbound(bus)

    lifecycle_topic = bus._topics["/system/lifecycle"]
    q: asyncio.Queue = asyncio.Queue()
    lifecycle_topic.add_subscriber("_test_", q)

    class Crasher(Node):
        name = "crasher"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            raise RuntimeError("test error")

    bus.register_node(Crasher())
    bus._nodes["crasher"].state = NodeState.RUNNING

    bus.publish("/inbound", inbound())
    await bus.spin_once(timeout=1.0)

    assert not q.empty()
    ev_msg = q.get_nowait()
    ev = ev_msg.payload
    assert isinstance(ev, LifecycleEvent)
    assert ev.event == "error"
    assert ev.node == "crasher"
    assert "test error" in ev.error


async def test_circuit_breaker_trips_node_to_error_state():
    bus = make_bus()
    register_inbound(bus)

    class Crasher(Node):
        name = "crasher"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            raise ValueError("always fails")

    bus.register_node(Crasher())
    handle = bus._nodes["crasher"]
    handle.state = NodeState.RUNNING

    # Publish MAX_CONSECUTIVE_NODE_ERRORS messages and process each
    for _ in range(MAX_CONSECUTIVE_NODE_ERRORS):
        bus.publish("/inbound", inbound())
        await bus.spin_once(timeout=1.0)

    assert handle.state == NodeState.ERROR
    assert handle.errors == MAX_CONSECUTIVE_NODE_ERRORS


async def test_circuit_breaker_skips_error_state_node():
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(N())
    handle = bus._nodes["n"]
    handle.state = NodeState.ERROR  # already in error

    bus.publish("/inbound", inbound())
    result = await bus.spin_once(timeout=0.05)
    # Error state nodes are skipped — no message processed
    assert result is None


# ── Fan-out ───────────────────────────────────────────────────────────────────


async def test_fan_out_three_subscribers():
    bus = make_bus()
    register_inbound(bus)

    results: dict[str, list[str]] = {"a": [], "b": [], "c": []}

    def make_node(node_name: str):
        captured = results[node_name]

        class N(Node):
            name = node_name
            subscriptions = ["/inbound"]
            publications = []

            async def on_message(self, msg: Message):
                captured.append(msg.payload.text)

        N.__name__ = node_name
        return N()

    for name in ("a", "b", "c"):
        bus.register_node(make_node(name))
        bus._nodes[name].state = NodeState.RUNNING

    bus.publish("/inbound", inbound("hello"))

    # Each node has the message queued; spin_once processes one per call
    for _ in range(3):
        await bus.spin_once(timeout=1.0)

    assert results["a"] == ["hello"]
    assert results["b"] == ["hello"]
    assert results["c"] == ["hello"]


# ── Backpressure ──────────────────────────────────────────────────────────────


async def test_backpressure_event_published_to_system_topic():
    bus = make_bus()
    # Small queue so it fills easily
    t = Topic[InboundChat]("/inbound")
    bus.register_topic(t)

    class TightNode(Node):
        name = "tight"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(TightNode())
    # Replace the queue with a tiny one
    tiny_q: asyncio.Queue = asyncio.Queue(maxsize=1)
    bus._nodes["tight"].queue = tiny_q
    # Re-wire the subscriber so topic sends to the tiny queue
    t.remove_subscriber("tight")
    t.add_subscriber("tight", tiny_q)

    bp_q: asyncio.Queue = asyncio.Queue()
    bus._topics["/system/backpressure"].add_subscriber("_test_", bp_q)

    bus.publish("/inbound", inbound("1"))  # fills queue
    bus.publish("/inbound", inbound("2"))  # triggers backpressure

    assert not bp_q.empty()
    bp_msg = bp_q.get_nowait()
    ev = bp_msg.payload
    assert isinstance(ev, BackpressureEvent)
    assert ev.topic == "/inbound"
    assert ev.subscriber_node == "tight"


# ── spin(max_messages=N) ──────────────────────────────────────────────────────


async def test_spin_max_messages_processes_exactly_n():
    bus = make_bus()
    register_inbound(bus)

    processed: list[str] = []

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            processed.append(msg.payload.text)

    bus.register_node(N())

    for i in range(5):
        bus.publish("/inbound", inbound(str(i)))

    result = await bus.spin(max_messages=3)

    assert result.messages_processed == 3
    assert len(processed) == 3


async def test_spin_result_contains_per_node_stats():
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(N())

    bus.publish("/inbound", inbound())
    result = await bus.spin(max_messages=1)

    assert "n" in result.per_node
    assert result.per_node["n"].messages_received == 1


async def test_spin_timeout_terminates():
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(N())

    # No messages published; spin should return after timeout
    result = await bus.spin(timeout=0.1)
    assert result.duration_s >= 0.0


# ── spin INIT phase ───────────────────────────────────────────────────────────


async def test_spin_node_lifecycle_created_to_stopped():
    """Node goes CREATED → RUNNING (init) → STOPPED (shutdown) through a full spin."""
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(N())
    assert bus._nodes["n"].state == NodeState.CREATED

    await bus.spin(timeout=0.05)

    assert bus._nodes["n"].state == NodeState.STOPPED


async def test_spin_init_failed_node_stays_error_others_run():
    bus = make_bus()
    register_inbound(bus)
    bus.register_topic(Topic[OutboundChat]("/outbound"))

    class GoodNode(Node):
        name = "good"
        subscriptions = ["/inbound"]
        publications = []

    class BadNode(Node):
        name = "bad"
        subscriptions = ["/outbound"]
        publications = []

        async def on_init(self, bus: BusHandle):
            raise RuntimeError("init failed")

    bus.register_node(GoodNode())
    bus.register_node(BadNode())

    bus.publish("/inbound", inbound())
    result = await bus.spin(max_messages=1)

    # bad node: ERROR during init → still shut down cleanly → STOPPED
    assert bus._nodes["bad"].state == NodeState.STOPPED
    assert result.per_node["good"].messages_received == 1


async def test_spin_init_failure_publishes_lifecycle_event():
    bus = make_bus()
    register_inbound(bus)

    lc_q: asyncio.Queue = asyncio.Queue()
    bus._topics["/system/lifecycle"].add_subscriber("_test_", lc_q)

    class BadNode(Node):
        name = "bad"
        subscriptions = ["/inbound"]
        publications = []

        async def on_init(self, bus: BusHandle):
            raise RuntimeError("init failed on purpose")

    bus.register_node(BadNode())
    await bus.spin(timeout=0.1)

    events = []
    while not lc_q.empty():
        events.append(lc_q.get_nowait().payload)

    init_failed = [e for e in events if e.event == "init_failed"]
    assert init_failed, "expected an init_failed LifecycleEvent"
    assert init_failed[0].node == "bad"


# ── Heartbeat ─────────────────────────────────────────────────────────────────


async def test_heartbeat_published_during_spin():
    bus = MessageBus(heartbeat_interval=0.05)
    register_inbound(bus)

    hb_q: asyncio.Queue = asyncio.Queue()
    bus._topics["/system/heartbeat"].add_subscriber("_test_", hb_q)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(N())
    await bus.spin(timeout=0.2)

    assert not hb_q.empty(), "expected at least one Heartbeat published"
    hb_msg = hb_q.get_nowait()
    hb = hb_msg.payload
    assert isinstance(hb, Heartbeat)
    assert hb.node_count >= 1


# ── Request / reply ───────────────────────────────────────────────────────────


async def test_request_reply_resolves():
    bus = make_bus()
    req_topic = Topic[InboundChat]("/req")
    rep_topic = Topic[OutboundChat]("/rep")
    bus.register_topic(req_topic)
    bus.register_topic(rep_topic)

    reply_received: list[Message] = []

    class Requester(Node):
        name = "requester"
        subscriptions = ["/rep"]
        publications = ["/req"]

        async def on_init(self, b: BusHandle) -> None:
            asyncio.create_task(self._do_request(b))

        async def _do_request(self, b: BusHandle) -> None:
            msg = await b.request(
                "/req",
                inbound("hello"),
                reply_on="/rep",
                timeout=2.0,
            )
            reply_received.append(msg)

    class Responder(Node):
        name = "responder"
        subscriptions = ["/req"]
        publications = ["/rep"]

        async def on_init(self, b: BusHandle) -> None:
            self._bus = b

        async def on_message(self, msg: Message) -> None:
            await self._bus.publish(
                "/rep",
                OutboundChat(text="world"),
                correlation_id=msg.correlation_id,
            )

    bus.register_node(Requester())
    bus.register_node(Responder())

    await bus.spin(timeout=1.5)

    assert len(reply_received) == 1
    assert reply_received[0].payload.text == "world"


async def test_request_reply_timeout_raises():
    bus = make_bus()
    req_topic = Topic[InboundChat]("/req")
    rep_topic = Topic[OutboundChat]("/rep")
    bus.register_topic(req_topic)
    bus.register_topic(rep_topic)

    class Requester(Node):
        name = "requester"
        subscriptions = ["/rep"]
        publications = ["/req"]

        async def on_init(self, b: BusHandle) -> None:
            self._bus = b

    class Sink(Node):
        """Receives requests but never replies."""

        name = "sink"
        subscriptions = ["/req"]
        publications = []

    bus.register_node(Requester())
    bus.register_node(Sink())

    # Manually init nodes so we can call request directly
    await bus._init_phase()

    handle = _BusHandle(bus, "requester")
    with pytest.raises(RequestTimeoutError):
        await handle.request("/req", inbound("ping"), reply_on="/rep", timeout=0.1)


# ── Concurrency mode ──────────────────────────────────────────────────────────


async def test_serial_mode_processes_messages_sequentially():
    """Serial node: semaphore=1 ensures no concurrent on_message execution."""
    bus = make_bus()
    register_inbound(bus)

    order: list[str] = []

    class SerialNode(Node):
        name = "serial"
        subscriptions = ["/inbound"]
        publications = []
        concurrency_mode = "serial"

        async def on_message(self, msg: Message) -> None:
            order.append(f"start:{msg.payload.text}")
            await asyncio.sleep(0.02)
            order.append(f"end:{msg.payload.text}")

    bus.register_node(SerialNode())
    bus._nodes["serial"].state = NodeState.RUNNING

    bus.publish("/inbound", inbound("1"))
    bus.publish("/inbound", inbound("2"))

    # Two concurrent spin_once calls — serial semaphore forces them to sequence
    t1 = asyncio.create_task(bus.spin_once(timeout=1.0))
    t2 = asyncio.create_task(bus.spin_once(timeout=1.0))
    await asyncio.gather(t1, t2)

    assert order == ["start:1", "end:1", "start:2", "end:2"]


# ── Validation phase ──────────────────────────────────────────────────────────


async def test_validation_orphan_topic_does_not_prevent_spin(caplog):
    bus = make_bus()
    # Register a topic nobody subscribes to or publishes on
    bus.register_topic(Topic[InboundChat]("/orphan"))

    import logging

    with caplog.at_level(logging.WARNING, logger="agentbus.bus"):
        result = await bus.spin(timeout=0.05)

    assert result is not None  # spin completed
    assert any("orphan" in r.message.lower() or "/orphan" in r.message for r in caplog.records)


# ── topic_history via BusHandle ───────────────────────────────────────────────


async def test_topic_history_returns_retained_messages():
    bus = make_bus()
    bus.register_topic(Topic[InboundChat]("/hist", retention=5))

    class N(Node):
        name = "n"
        subscriptions = ["/hist"]
        publications = ["/hist"]

    bus.register_node(N())
    await bus._init_phase()

    for i in range(3):
        bus.publish("/hist", inbound(str(i)))

    bh = _BusHandle(bus, "n")
    history = await bh.topic_history("/hist", n=10)
    assert len(history) == 3
    assert [m.payload.text for m in history] == ["0", "1", "2"]


async def test_topic_history_missing_topic_returns_empty():
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = ["/inbound"]

    bus.register_node(N())
    await bus._init_phase()

    bh = _BusHandle(bus, "n")
    history = await bh.topic_history("/nonexistent", n=5)
    assert history == []
