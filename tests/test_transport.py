"""Tests for Layer 4 — Transport (InMemoryTransport over a BusCore)."""

import asyncio

import pytest
from pydantic import BaseModel

from agentbus.contracts import Envelope, register_schema
from agentbus.core import BusCore
from agentbus.message import Message
from agentbus.transport import InMemoryTransport, LocalTarget, Transport


class _P(BaseModel):
    n: int


def _core() -> BusCore:
    reg: dict[str, type[BaseModel]] = {}
    register_schema("/a", _P, registry=reg)
    register_schema("/b", _P, registry=reg)
    return BusCore(schemas=reg)


def _env(topic: str, n: int) -> Envelope:
    return Envelope(source_node="c", topic=topic, payload={"n": n})


def test_inmemory_satisfies_transport_protocol():
    assert isinstance(InMemoryTransport(_core()), Transport)


def test_buscore_satisfies_localtarget_protocol():
    assert isinstance(_core(), LocalTarget)


async def test_send_publishes_to_core():
    core = _core()
    t = InMemoryTransport(core)
    await t.connect()
    await t.send(_env("/a", 1))
    assert [m.payload.n for m in core.log] == [1]


async def test_subscribe_receives_matching_envelopes():
    core = _core()
    t = InMemoryTransport(core)
    await t.connect()
    await t.announce_subscription("/a")

    core.publish(Message(source_node="x", topic="/a", payload=_P(n=7)))

    agen = t.receive()
    env = await asyncio.wait_for(agen.__anext__(), timeout=1)
    assert env.topic == "/a"
    assert env.payload == {"n": 7}


async def test_send_revalidates_payload_at_boundary():
    core = _core()
    t = InMemoryTransport(core)
    await t.connect()
    from agentbus.errors import TopicSchemaError

    bad = Envelope(source_node="rogue", topic="/a", payload={"n": "not-int"})
    with pytest.raises(TopicSchemaError):
        await t.send(bad)


async def test_send_before_connect_raises():
    t = InMemoryTransport(_core())
    with pytest.raises(RuntimeError, match="not connected"):
        await t.send(_env("/a", 1))


async def test_close_unblocks_receive():
    core = _core()
    t = InMemoryTransport(core)
    await t.connect()
    await t.announce_subscription("/a")

    received: list[Envelope] = []

    async def drain():
        async for env in t.receive():
            received.append(env)

    task = asyncio.create_task(drain())
    await asyncio.sleep(0.01)
    await t.close()
    await asyncio.wait_for(task, timeout=1)  # generator exits on close
    assert received == []


async def test_close_unsubscribes_from_core():
    core = _core()
    t = InMemoryTransport(core)
    await t.connect()
    await t.announce_subscription("/a")
    assert len(core._subs) == 1
    await t.close()
    assert len(core._subs) == 0


async def test_inmemory_against_message_bus_reaches_nodes():
    from agentbus.bus import MessageBus
    from agentbus.node import Node
    from agentbus.topic import Topic

    seen: list[int] = []

    class _Collector(Node):
        name = "collector"
        subscriptions = ["/a"]

        async def on_message(self, msg: Message):
            seen.append(msg.payload.n)

    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[_P]("/a"))
    bus.register_node(_Collector())

    t = InMemoryTransport(bus.local_target())
    await t.connect()
    await t.send(_env("/a", 3))  # client publish reaches an in-process node
    await bus.spin_once(timeout=0.2)
    assert seen == [3]
