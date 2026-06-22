"""Tests for Layer 5 — BusClient (publish/subscribe/request over a transport)."""

import asyncio

import pytest
from pydantic import BaseModel

from agentbus.client import BusClient
from agentbus.contracts import register_schema
from agentbus.core import BusCore, CallbackSink
from agentbus.errors import RequestTimeoutError
from agentbus.message import Message
from agentbus.transport import InMemoryTransport


class _P(BaseModel):
    n: int


def _core() -> BusCore:
    reg: dict[str, type[BaseModel]] = {}
    for t in ("/a", "/b", "/req", "/rep"):
        register_schema(t, _P, registry=reg)
    return BusCore(schemas=reg)


def _client(core: BusCore, name: str = "agent") -> BusClient:
    return BusClient(InMemoryTransport(core), name=name)


async def test_publish_and_subscribe_round_trip():
    core = _core()
    client = _client(core)
    await client.connect()
    q = await client.subscribe("/a")
    await client.publish("/a", _P(n=5))
    env = await asyncio.wait_for(q.get(), timeout=1)
    assert env.payload == {"n": 5}
    assert env.source_node == "agent"
    await client.close()


async def test_subscribe_is_idempotent():
    core = _core()
    client = _client(core)
    await client.connect()
    q1 = await client.subscribe("/a")
    q2 = await client.subscribe("/a")
    assert q1 is q2
    assert len(core._subs) == 1
    await client.close()


async def test_wildcard_subscription():
    core = _core()
    client = _client(core)
    await client.connect()
    q = await client.subscribe("/**")
    await client.publish("/a", _P(n=1))
    await client.publish("/b", _P(n=2))
    e1 = await asyncio.wait_for(q.get(), timeout=1)
    e2 = await asyncio.wait_for(q.get(), timeout=1)
    assert {e1.topic, e2.topic} == {"/a", "/b"}
    await client.close()


async def test_two_clients_share_a_bus():
    core = _core()
    a = _client(core, "a")
    b = _client(core, "b")
    await a.connect()
    await b.connect()
    qb = await b.subscribe("/a")
    await a.publish("/a", _P(n=99))
    env = await asyncio.wait_for(qb.get(), timeout=1)
    assert env.payload == {"n": 99}
    assert env.source_node == "a"
    await a.close()
    await b.close()


async def test_request_reply_via_correlation_id():
    core = _core()
    client = _client(core, "requester")

    # A responder: any request on /req gets a reply on its reply_to with the
    # same correlation_id. Runs synchronously inside core fan-out.
    def respond(msg: Message) -> None:
        core.publish(
            Message(
                source_node="responder",
                topic=msg.reply_to,
                correlation_id=msg.correlation_id,
                payload=_P(n=msg.payload.n + 1),
            )
        )

    core.subscribe("/req", CallbackSink(respond))

    await client.connect()
    reply = await client.request("/req", _P(n=10), reply_on="/rep", timeout=1)
    assert reply.payload == {"n": 11}
    assert reply.source_node == "responder"
    await client.close()


async def test_request_times_out_with_no_responder():
    core = _core()
    client = _client(core, "requester")
    await client.connect()
    with pytest.raises(RequestTimeoutError):
        await client.request("/req", _P(n=1), reply_on="/rep", timeout=0.2)
    await client.close()


async def test_request_does_not_leak_pending_after_timeout():
    core = _core()
    client = _client(core, "requester")
    await client.connect()
    with pytest.raises(RequestTimeoutError):
        await client.request("/req", _P(n=1), reply_on="/rep", timeout=0.2)
    assert client._pending == {}
    await client.close()


async def test_close_cancels_outstanding_requests():
    core = _core()
    client = _client(core, "requester")
    await client.connect()

    async def do_request():
        return await client.request("/req", _P(n=1), reply_on="/rep", timeout=5)

    task = asyncio.create_task(do_request())
    await asyncio.sleep(0.05)
    await client.close()
    with pytest.raises((asyncio.CancelledError, RequestTimeoutError)):
        await task


async def test_client_over_message_bus_full_pipeline():
    from agentbus.bus import MessageBus
    from agentbus.node import Node
    from agentbus.topic import Topic

    class _Echo(Node):
        name = "echo"
        subscriptions = ["/req"]
        publications = ["/rep"]

        async def on_init(self, bus):
            self._bus = bus

        async def on_message(self, msg: Message):
            await self._bus.publish(
                "/rep", _P(n=msg.payload.n * 2), correlation_id=msg.correlation_id
            )

    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[_P]("/req"))
    bus.register_topic(Topic[_P]("/rep"))
    bus.register_node(_Echo())

    client = BusClient(InMemoryTransport(bus.local_target()), name="caller")
    await client.connect()

    async def drive():
        # The node processes exactly one message (/req) and replies on /rep,
        # which no node consumes — so only one node-message is ever processed.
        await bus.spin(max_messages=1)

    spin_task = asyncio.create_task(drive())
    reply = await client.request("/req", _P(n=4), reply_on="/rep", timeout=2)
    assert reply.payload == {"n": 8}
    await client.close()
    await asyncio.wait_for(spin_task, timeout=2)
