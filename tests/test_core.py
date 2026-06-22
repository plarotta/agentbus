"""Tests for Layer 2 — BusCore (log + subscriptions + hooks + fan-out)."""

import asyncio

import pytest
from pydantic import BaseModel

from agentbus.contracts import register_schema
from agentbus.core import BusCore, CallbackSink, QueueSink, Sink, SinkClosed
from agentbus.errors import TopicSchemaError
from agentbus.message import Message


class _P(BaseModel):
    n: int


def _registry() -> dict:
    reg: dict[str, type[BaseModel]] = {}
    register_schema("/a", _P, registry=reg)
    register_schema("/b", _P, registry=reg)
    register_schema("/x/y", _P, registry=reg)
    return reg


def _msg(topic: str, n: int = 1) -> Message:
    return Message(source_node="t", topic=topic, payload=_P(n=n))


def test_publish_fans_out_to_matching_sink():
    core = BusCore(schemas=_registry())
    got: list[Message] = []
    core.subscribe("/a", CallbackSink(got.append))
    core.publish(_msg("/a"))
    assert len(got) == 1
    assert got[0].topic == "/a"


def test_publish_skips_nonmatching_sink():
    core = BusCore(schemas=_registry())
    got: list[Message] = []
    core.subscribe("/b", CallbackSink(got.append))
    core.publish(_msg("/a"))
    assert got == []


def test_wildcard_subscription_matches_at_publish_time():
    core = BusCore(schemas=_registry())
    got: list[Message] = []
    core.subscribe("/**", CallbackSink(got.append))
    core.publish(_msg("/a"))
    core.publish(_msg("/x/y"))
    assert [m.topic for m in got] == ["/a", "/x/y"]


def test_unsubscribe_stops_delivery():
    core = BusCore(schemas=_registry())
    got: list[Message] = []
    unsub = core.subscribe("/a", CallbackSink(got.append))
    core.publish(_msg("/a"))
    unsub()
    core.publish(_msg("/a"))
    assert len(got) == 1


def test_log_records_before_fanout():
    core = BusCore(schemas=_registry())
    core.publish(_msg("/a", 1))
    core.publish(_msg("/b", 2))
    assert [m.topic for m in core.log] == ["/a", "/b"]


def test_hook_can_block_message():
    core = BusCore(schemas=_registry())
    got: list[Message] = []
    core.subscribe("/a", CallbackSink(got.append))
    core.add_hook(lambda m: None if m.topic == "/a" else m)
    result = core.publish(_msg("/a"))
    assert result is None
    assert got == []
    assert len(core.log) == 0  # blocked before the log


def test_hook_can_transform_message():
    core = BusCore(schemas=_registry())
    got: list[Message] = []
    core.subscribe("/a", CallbackSink(got.append))

    def redact(m: Message) -> Message:
        return m.model_copy(update={"payload": _P(n=999)})

    core.add_hook(redact)
    core.publish(_msg("/a", 1))
    assert got[0].payload.n == 999
    assert core.log[0].payload.n == 999  # log sees the transformed envelope


def test_remove_hook():
    core = BusCore(schemas=_registry())
    remove = core.add_hook(lambda m: None)
    remove()
    assert core.publish(_msg("/a")) is not None


def test_validate_rejects_wrong_payload_type():
    core = BusCore(schemas=_registry())

    class _Other(BaseModel):
        z: int

    bad = Message(source_node="t", topic="/a", payload=_Other(z=1))
    with pytest.raises(TopicSchemaError):
        core.publish(bad)


def test_validate_passes_unregistered_topic_through():
    core = BusCore(schemas={})  # empty registry
    got: list[Message] = []
    core.subscribe("/a", CallbackSink(got.append))
    core.publish(_msg("/a"))  # no schema → not validated, still delivered
    assert len(got) == 1


def test_closed_sink_is_pruned():
    core = BusCore(schemas=_registry())

    class _Dead:
        def deliver(self, msg: Message) -> None:
            raise SinkClosed("gone")

    live: list[Message] = []
    core.subscribe("/a", _Dead())
    core.subscribe("/a", CallbackSink(live.append))
    core.publish(_msg("/a"))
    # Dead sink pruned; live one still delivered.
    assert len(live) == 1
    assert len(core._subs) == 1
    core.publish(_msg("/a"))
    assert len(live) == 2


def test_sink_exception_does_not_break_fanout():
    core = BusCore(schemas=_registry())

    def boom(m: Message) -> None:
        raise ValueError("boom")

    got: list[Message] = []
    core.subscribe("/a", CallbackSink(boom))
    core.subscribe("/a", CallbackSink(got.append))
    core.publish(_msg("/a"))
    assert len(got) == 1  # second sink unaffected


def test_sink_protocol_runtime_checkable():
    assert isinstance(CallbackSink(lambda m: None), Sink)


async def test_queue_sink_delivers():
    core = BusCore(schemas=_registry())
    q: asyncio.Queue = asyncio.Queue()
    core.subscribe("/a", QueueSink(q))
    core.publish(_msg("/a", 5))
    msg = await asyncio.wait_for(q.get(), timeout=1)
    assert msg.payload.n == 5


async def test_queue_sink_drop_oldest_on_overflow():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    dropped: list[Message] = []
    sink = QueueSink(q, policy="drop-oldest", on_drop=dropped.append)
    sink.deliver(_msg("/a", 1))
    sink.deliver(_msg("/a", 2))  # evicts n=1
    assert [d.payload.n for d in dropped] == [1]
    remaining = await q.get()
    assert remaining.payload.n == 2


async def test_queue_sink_drop_newest_on_overflow():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    dropped: list[Message] = []
    sink = QueueSink(q, policy="drop-newest", on_drop=dropped.append)
    sink.deliver(_msg("/a", 1))
    sink.deliver(_msg("/a", 2))  # discards n=2
    assert [d.payload.n for d in dropped] == [2]
    remaining = await q.get()
    assert remaining.payload.n == 1


async def test_queue_sink_close_raises_sinkclosed():
    q: asyncio.Queue = asyncio.Queue()
    sink = QueueSink(q)
    sink.close()
    with pytest.raises(SinkClosed):
        sink.deliver(_msg("/a"))


def test_publish_stamps_monotonic_offsets():
    core = BusCore(schemas=_registry())
    a = core.publish(_msg("/a", 1))
    b = core.publish(_msg("/a", 2))
    assert a.offset == 1
    assert b.offset == 2
    assert core.latest_offset() == 2


def test_replay_from_offset_is_exclusive():
    core = BusCore(schemas=_registry())
    for i in range(5):
        core.publish(_msg("/a", i))
    out = list(core.replay(from_offset=3))
    assert [m.payload.n for m in out] == [3, 4]  # offsets 4, 5
    assert [m.offset for m in out] == [4, 5]


def test_replay_filters_by_topic():
    core = BusCore(schemas=_registry())
    core.publish(_msg("/a", 1))
    core.publish(_msg("/b", 2))
    core.publish(_msg("/a", 3))
    out = list(core.replay(topic="/a"))
    assert [m.payload.n for m in out] == [1, 3]


def test_replay_respects_limit():
    core = BusCore(schemas=_registry())
    for i in range(5):
        core.publish(_msg("/a", i))
    out = list(core.replay(limit=2))
    assert [m.payload.n for m in out] == [0, 1]
