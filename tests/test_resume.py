"""Resume-after-kill: a client reconnects with from_offset and catches up
gap-free, then continues live. Covers in-memory and durable (SQLite+WS) paths.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest_asyncio
from pydantic import BaseModel

from agentbus.client import BusClient
from agentbus.contracts import register_schema
from agentbus.core import BusCore
from agentbus.journal import SqliteLog
from agentbus.message import Message
from agentbus.server import BusServer
from agentbus.transport import InMemoryTransport, WebSocketTransport


class _P(BaseModel):
    n: int


def _schemas() -> dict:
    reg: dict[str, type[BaseModel]] = {}
    register_schema("/a", _P, registry=reg)
    return reg


def _msg(n: int) -> Message:
    return Message(source_node="src", topic="/a", payload=_P(n=n))


# ── In-memory resume ────────────────────────────────────────────────────────


async def test_inmemory_resume_catches_up_then_live():
    core = BusCore(schemas=_schemas())
    # History accrues before the client ever connects.
    for i in range(3):
        core.publish(_msg(i))  # offsets 1, 2, 3

    client = BusClient(InMemoryTransport(core), name="c")
    await client.connect()
    # Resume from offset 1 → expect offsets 2, 3 replayed.
    q = await client.subscribe("/a", from_offset=1)
    e2 = await asyncio.wait_for(q.get(), timeout=1)
    e3 = await asyncio.wait_for(q.get(), timeout=1)
    assert (e2.offset, e2.payload["n"]) == (2, 1)
    assert (e3.offset, e3.payload["n"]) == (3, 2)

    # Now publish live → continues with no gap or duplicate.
    core.publish(_msg(99))  # offset 4
    e4 = await asyncio.wait_for(q.get(), timeout=1)
    assert (e4.offset, e4.payload["n"]) == (4, 99)
    assert q.empty()
    await client.close()


async def test_inmemory_resume_from_zero_replays_all():
    core = BusCore(schemas=_schemas())
    core.publish(_msg(0))
    core.publish(_msg(1))
    client = BusClient(InMemoryTransport(core), name="c")
    await client.connect()
    q = await client.subscribe("/a", from_offset=0)
    got = [await asyncio.wait_for(q.get(), timeout=1) for _ in range(2)]
    assert [e.offset for e in got] == [1, 2]
    await client.close()


# ── Durable resume across a server restart (SQLite + WebSocket) ──────────────


@pytest_asyncio.fixture
def db_path():
    d = tempfile.mkdtemp(dir="/tmp")
    yield Path(d) / "journal.db"


async def test_durable_resume_after_server_restart(db_path):
    schemas = _schemas()

    # ── First server instance: client processes up to offset 2, then "crashes".
    log1 = SqliteLog(db_path, schemas=schemas)
    core1 = BusCore(schemas=schemas, log_store=log1)
    srv1 = BusServer(core1)
    await srv1.start()
    url = srv1.url

    for i in range(3):
        core1.publish(_msg(i))  # offsets 1, 2, 3 (persisted before the client reads)
    client1 = BusClient(WebSocketTransport(url), name="c")
    await client1.connect()
    # Resume-from-0 for a deterministic read (no live-subscribe ack race).
    q1 = await client1.subscribe("/a", from_offset=0)
    last_processed = 0
    for _ in range(2):  # only consume offsets 1 and 2, then "crash"
        env = await asyncio.wait_for(q1.get(), timeout=2)
        last_processed = env.offset
    assert last_processed == 2
    await client1.close()
    await srv1.stop()
    log1.close()

    # More messages arrive while the client is down (offset 4), persisted.
    log_down = SqliteLog(db_path, schemas=schemas)
    log_down.append(_msg(100))  # offset 4
    log_down.close()

    # ── Restart: new server over the SAME file; client resumes from its cursor.
    log2 = SqliteLog(db_path, schemas=schemas)
    core2 = BusCore(schemas=schemas, log_store=log2)
    srv2 = BusServer(core2)
    await srv2.start()
    try:
        client2 = BusClient(WebSocketTransport(srv2.url), name="c")
        await client2.connect()
        q2 = await client2.subscribe("/a", from_offset=last_processed)
        # Expect everything after offset 2: offset 3 (n=2) and offset 4 (n=100).
        e3 = await asyncio.wait_for(q2.get(), timeout=2)
        e4 = await asyncio.wait_for(q2.get(), timeout=2)
        assert (e3.offset, e3.payload["n"]) == (3, 2)
        assert (e4.offset, e4.payload["n"]) == (4, 100)

        # And live delivery continues past the replayed backlog.
        core2.publish(_msg(5))  # offset 5
        e5 = await asyncio.wait_for(q2.get(), timeout=2)
        assert e5.offset == 5
        await client2.close()
    finally:
        await srv2.stop()
        log2.close()


async def test_no_gap_between_replay_and_live(db_path):
    # Publish concurrently around the subscribe to ensure the atomic
    # snapshot+attach leaves no hole between replayed and live messages.
    schemas = _schemas()
    log = SqliteLog(db_path, schemas=schemas)
    core = BusCore(schemas=schemas, log_store=log)
    srv = BusServer(core)
    await srv.start()
    try:
        for i in range(5):
            core.publish(_msg(i))  # offsets 1..5
        client = BusClient(WebSocketTransport(srv.url), name="c")
        await client.connect()
        q = await client.subscribe("/a", from_offset=0)
        for i in range(5, 10):
            core.publish(_msg(i))  # offsets 6..10 (live)
        seen = [await asyncio.wait_for(q.get(), timeout=2) for _ in range(10)]
        assert [e.offset for e in seen] == list(range(1, 11))  # contiguous, no gap/dupe
        await client.close()
    finally:
        await srv.stop()
        log.close()
