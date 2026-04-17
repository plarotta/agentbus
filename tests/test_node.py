import pytest

from agentbus.message import Message
from agentbus.node import BusHandle, Node, NodeHandle, NodeState
from agentbus.schemas.common import InboundChat

# ── helpers ───────────────────────────────────────────────────────────────────


class MinimalNode(Node):
    name = "minimal"


class FullNode(Node):
    name = "full"
    subscriptions = ["/inbound/chat", "/system/*"]
    publications = ["/tools/request"]
    concurrency = 4
    concurrency_mode = "parallel"


class SerialNode(Node):
    name = "serial-worker"
    concurrency = 8  # ignored in serial mode
    concurrency_mode = "serial"


# ── subclass declaration ──────────────────────────────────────────────────────


def test_minimal_node_has_required_name():
    assert MinimalNode.name == "minimal"


def test_defaults_are_correct():
    assert MinimalNode.subscriptions == []
    assert MinimalNode.publications == []
    assert MinimalNode.concurrency == 1
    assert MinimalNode.concurrency_mode == "parallel"


def test_full_node_overrides_all_fields():
    assert FullNode.subscriptions == ["/inbound/chat", "/system/*"]
    assert FullNode.publications == ["/tools/request"]
    assert FullNode.concurrency == 4
    assert FullNode.concurrency_mode == "parallel"


def test_node_without_name_raises_on_access():
    """Accessing .name on a Node subclass that omits it raises AttributeError."""

    class Unnamed(Node):
        pass

    with pytest.raises(AttributeError):
        _ = Unnamed.name


def test_subclass_defaults_are_independent():
    """Mutating one node's subscriptions list doesn't affect another node's list."""

    class NodeA(Node):
        name = "a"
        subscriptions = ["/foo"]

    class NodeB(Node):
        name = "b"
        subscriptions = ["/bar"]

    assert NodeA.subscriptions != NodeB.subscriptions


# ── no-op lifecycle hooks ─────────────────────────────────────────────────────


async def test_on_init_noop():
    node = MinimalNode()

    class _FakeBus:
        async def publish(self, topic, payload, correlation_id=None): ...
        async def request(self, topic, payload, reply_on, *, timeout=30.0): ...
        async def topic_history(self, topic, n=10):
            return []

    await node.on_init(_FakeBus())  # must not raise


async def test_on_message_noop():
    node = MinimalNode()
    msg = Message(
        source_node="bus",
        topic="/inbound/chat",
        payload=InboundChat(channel="cli", sender="user", text="hi"),
    )
    await node.on_message(msg)  # must not raise


async def test_on_shutdown_noop():
    node = MinimalNode()
    await node.on_shutdown()  # must not raise


# ── BusHandle protocol ────────────────────────────────────────────────────────


def test_fake_bus_satisfies_protocol():
    class FakeBus:
        async def publish(self, topic, payload, correlation_id=None): ...
        async def request(self, topic, payload, reply_on, *, timeout=30.0): ...
        async def topic_history(self, topic, n=10):
            return []

    assert isinstance(FakeBus(), BusHandle)


def test_incomplete_bus_does_not_satisfy_protocol():
    class IncompleteBus:
        async def publish(self, topic, payload): ...

        # missing request and topic_history

    assert not isinstance(IncompleteBus(), BusHandle)


# ── NodeHandle: queue ─────────────────────────────────────────────────────────


def test_node_handle_initial_state():
    handle = NodeHandle(MinimalNode())
    assert handle.state is NodeState.CREATED


def test_node_handle_default_queue_size():
    handle = NodeHandle(MinimalNode())
    assert handle.queue.maxsize == 100


def test_node_handle_custom_queue_size():
    handle = NodeHandle(MinimalNode(), queue_size=50)
    assert handle.queue.maxsize == 50


def test_node_handle_queue_is_empty_on_creation():
    handle = NodeHandle(MinimalNode())
    assert handle.queue.empty()


# ── NodeHandle: semaphore (concurrency_mode) ──────────────────────────────────


def test_parallel_mode_uses_node_concurrency():
    handle = NodeHandle(FullNode())
    # asyncio.Semaphore doesn't expose value directly; use internal attribute
    assert handle.semaphore._value == 4


def test_serial_mode_forces_semaphore_1():
    handle = NodeHandle(SerialNode())
    assert handle.semaphore._value == 1


def test_serial_mode_ignores_node_concurrency():
    # SerialNode.concurrency = 8, but serial mode must cap at 1
    handle = NodeHandle(SerialNode())
    assert handle.semaphore._value == 1


def test_default_concurrency_is_1():
    handle = NodeHandle(MinimalNode())
    assert handle.semaphore._value == 1


def test_zero_concurrency_clamped_to_1():
    class ZeroConcNode(Node):
        name = "zero"
        concurrency = 0

    handle = NodeHandle(ZeroConcNode())
    assert handle.semaphore._value == 1


# ── NodeState ─────────────────────────────────────────────────────────────────


def test_node_state_values():
    assert NodeState.CREATED.value == "CREATED"
    assert NodeState.RUNNING.value == "RUNNING"
    assert NodeState.STOPPED.value == "STOPPED"
    assert NodeState.ERROR.value == "ERROR"


def test_node_handle_state_is_mutable():
    handle = NodeHandle(MinimalNode())
    handle.state = NodeState.RUNNING
    assert handle.state is NodeState.RUNNING
