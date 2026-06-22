"""Tests for the WebSocket transport + BusServer (over a real local socket)."""

import asyncio

import pytest
import pytest_asyncio
from pydantic import BaseModel

from agentbus.client import BusClient
from agentbus.contracts import register_schema
from agentbus.core import BusCore, CallbackSink
from agentbus.errors import RequestTimeoutError
from agentbus.message import Message
from agentbus.server import BusServer
from agentbus.transport import Transport, WebSocketTransport


class _P(BaseModel):
    n: int


def _core() -> BusCore:
    reg: dict[str, type[BaseModel]] = {}
    for t in ("/a", "/b", "/req", "/rep"):
        register_schema(t, _P, registry=reg)
    return BusCore(schemas=reg)


@pytest_asyncio.fixture
async def server():
    srv = BusServer(_core())
    await srv.start()
    yield srv
    await srv.stop()


def _client(server: BusServer, name: str = "remote") -> BusClient:
    return BusClient(WebSocketTransport(server.url), name=name)


def test_ws_transport_satisfies_protocol():
    assert isinstance(WebSocketTransport("ws://localhost:1"), Transport)


def test_ws_transport_missing_dep_message(monkeypatch):
    import agentbus.transport as tp

    def _boom():
        raise RuntimeError(
            "WebSocket transport requires the 'websockets' package. "
            "Install it with: uv sync --extra ws"
        )

    monkeypatch.setattr(tp, "_require_websockets", _boom)
    with pytest.raises(RuntimeError, match="uv sync --extra ws"):
        WebSocketTransport("ws://localhost:1")


async def test_publish_subscribe_over_the_wire(server):
    client = _client(server)
    await client.connect()
    q = await client.subscribe("/a")
    await client.publish("/a", _P(n=7))
    env = await asyncio.wait_for(q.get(), timeout=2)
    assert env.payload == {"n": 7}
    assert env.source_node == "remote"
    await client.close()


async def test_two_ws_clients_share_a_bus(server):
    a = _client(server, "a")
    b = _client(server, "b")
    await a.connect()
    await b.connect()
    qb = await b.subscribe("/a")
    await asyncio.sleep(0.05)  # let b's subscribe register server-side
    await a.publish("/a", _P(n=42))
    env = await asyncio.wait_for(qb.get(), timeout=2)
    assert env.payload == {"n": 42}
    assert env.source_node == "a"
    await a.close()
    await b.close()


async def test_wildcard_subscription_over_the_wire(server):
    client = _client(server)
    await client.connect()
    q = await client.subscribe("/**")
    await client.publish("/a", _P(n=1))
    await client.publish("/b", _P(n=2))
    e1 = await asyncio.wait_for(q.get(), timeout=2)
    e2 = await asyncio.wait_for(q.get(), timeout=2)
    assert {e1.topic, e2.topic} == {"/a", "/b"}
    await client.close()


async def test_request_reply_over_the_wire():
    # Server-side responder: replies on /rep with the request's correlation_id.
    core = _core()

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
    srv = BusServer(core)
    await srv.start()
    try:
        client = BusClient(WebSocketTransport(srv.url), name="requester")
        await client.connect()
        reply = await client.request("/req", _P(n=10), reply_on="/rep", timeout=2)
        assert reply.payload == {"n": 11}
        assert reply.source_node == "responder"
        await client.close()
    finally:
        await srv.stop()


async def test_request_times_out_with_no_responder(server):
    client = _client(server)
    await client.connect()
    with pytest.raises(RequestTimeoutError):
        await client.request("/req", _P(n=1), reply_on="/rep", timeout=0.3)
    await client.close()


async def test_invalid_payload_dropped_not_fatal(server):
    # A raw frame with a bad payload must not kill the connection; a valid
    # publish right after still goes through.
    client = _client(server)
    await client.connect()
    q = await client.subscribe("/a")
    # Reach into the transport to send a hand-crafted bad publish.
    import json

    await client.t._conn.send(  # type: ignore[attr-defined]
        json.dumps(
            {"op": "publish", "env": {"source_node": "x", "topic": "/a", "payload": {"n": "bad"}}}
        )
    )
    await client.publish("/a", _P(n=5))
    env = await asyncio.wait_for(q.get(), timeout=2)
    assert env.payload == {"n": 5}
    await client.close()


async def test_close_then_publish_raises(server):
    client = _client(server)
    await client.connect()
    await client.close()
    with pytest.raises(RuntimeError, match="not connected"):
        await client.publish("/a", _P(n=1))
