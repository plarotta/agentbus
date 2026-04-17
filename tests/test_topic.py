import asyncio

import pytest

from agentbus.errors import TopicSchemaError
from agentbus.message import Message
from agentbus.schemas.common import InboundChat, ToolRequest
from agentbus.topic import Topic

# ── helpers ───────────────────────────────────────────────────────────────────


def _chat_msg(text: str = "hi") -> Message:
    return Message(
        source_node="bus",
        topic="/inbound/chat",
        payload=InboundChat(channel="cli", sender="user", text=text),
    )


# ── schema binding ────────────────────────────────────────────────────────────


def test_schema_binding():
    topic = Topic[InboundChat]("/inbound/chat")
    assert topic.schema is InboundChat
    assert topic.name == "/inbound/chat"


def test_bare_topic_raises():
    with pytest.raises(TypeError, match="parameterized"):
        Topic("/tools/request")


def test_topic_name_and_description():
    topic = Topic[InboundChat]("/inbound/chat", description="incoming messages")
    assert topic.description == "incoming messages"
    assert topic.retention == 0


# ── subscriber management ─────────────────────────────────────────────────────


def test_add_and_remove_subscriber():
    topic = Topic[InboundChat]("/inbound/chat")
    q = asyncio.Queue(maxsize=10)
    topic.add_subscriber("node-a", q)
    assert "node-a" in topic._subscribers
    topic.remove_subscriber("node-a")
    assert "node-a" not in topic._subscribers


def test_remove_nonexistent_subscriber_is_noop():
    topic = Topic[InboundChat]("/inbound/chat")
    topic.remove_subscriber("ghost")  # must not raise


# ── fan-out ───────────────────────────────────────────────────────────────────


def test_put_delivers_to_all_subscribers():
    topic = Topic[InboundChat]("/inbound/chat")
    q1 = asyncio.Queue(maxsize=10)
    q2 = asyncio.Queue(maxsize=10)
    topic.add_subscriber("a", q1)
    topic.add_subscriber("b", q2)

    msg = _chat_msg()
    events = topic.put(msg)

    assert events == []
    assert q1.qsize() == 1
    assert q2.qsize() == 1
    assert q1.get_nowait() is msg
    assert q2.get_nowait() is msg


def test_put_with_no_subscribers_returns_empty():
    topic = Topic[InboundChat]("/inbound/chat")
    events = topic.put(_chat_msg())
    assert events == []


def test_removed_subscriber_receives_nothing():
    topic = Topic[InboundChat]("/inbound/chat")
    q = asyncio.Queue(maxsize=10)
    topic.add_subscriber("a", q)
    topic.remove_subscriber("a")
    topic.put(_chat_msg())
    assert q.empty()


# ── retention buffer ──────────────────────────────────────────────────────────


def test_retention_keeps_last_n():
    topic = Topic[InboundChat]("/inbound/chat", retention=3)
    msgs = [_chat_msg(str(i)) for i in range(5)]
    for m in msgs:
        topic.put(m)
    history = topic.history()
    assert len(history) == 3
    assert [m.payload.text for m in history] == ["2", "3", "4"]


def test_history_sliced_by_n():
    topic = Topic[InboundChat]("/inbound/chat", retention=5)
    for i in range(5):
        topic.put(_chat_msg(str(i)))
    assert len(topic.history(2)) == 2
    assert topic.history(2)[-1].payload.text == "4"


def test_no_retention_history_is_empty():
    topic = Topic[InboundChat]("/inbound/chat", retention=0)
    topic.put(_chat_msg())
    assert topic.history() == []


def test_history_all_when_n_is_none():
    topic = Topic[InboundChat]("/inbound/chat", retention=5)
    for i in range(4):
        topic.put(_chat_msg(str(i)))
    assert len(topic.history()) == 4


# ── schema validation ─────────────────────────────────────────────────────────


def test_validate_payload_correct_type():
    topic = Topic[InboundChat]("/inbound/chat")
    topic.validate_payload(InboundChat(channel="cli", sender="user", text="ok"))


def test_validate_payload_wrong_type_raises():
    topic = Topic[InboundChat]("/inbound/chat")
    with pytest.raises(TopicSchemaError):
        topic.validate_payload(ToolRequest(tool="browser"))


def test_validate_payload_error_message_contains_names():
    topic = Topic[InboundChat]("/inbound/chat")
    with pytest.raises(TopicSchemaError, match="InboundChat"):
        topic.validate_payload(ToolRequest(tool="browser"))


# ── backpressure: drop-oldest (default) ───────────────────────────────────────


def test_backpressure_drop_oldest_generates_event():
    topic = Topic[InboundChat]("/inbound/chat")  # default policy
    q = asyncio.Queue(maxsize=2)
    topic.add_subscriber("node-a", q)

    msgs = [_chat_msg(str(i)) for i in range(3)]
    topic.put(msgs[0])
    topic.put(msgs[1])
    events = topic.put(msgs[2])  # queue full → drop oldest

    assert len(events) == 1
    ev = events[0]
    assert ev.policy == "drop-oldest"
    assert ev.dropped_message_id == msgs[0].id
    assert ev.subscriber_node == "node-a"
    assert ev.topic == "/inbound/chat"
    assert q.qsize() == 2


def test_backpressure_drop_oldest_queue_contains_newer_messages():
    topic = Topic[InboundChat]("/inbound/chat")
    q = asyncio.Queue(maxsize=2)
    topic.add_subscriber("n", q)

    msgs = [_chat_msg(str(i)) for i in range(3)]
    topic.put(msgs[0])
    topic.put(msgs[1])
    topic.put(msgs[2])

    remaining = [q.get_nowait(), q.get_nowait()]
    assert remaining[0].payload.text == "1"
    assert remaining[1].payload.text == "2"


# ── backpressure: drop-newest ─────────────────────────────────────────────────


def test_backpressure_drop_newest_generates_event():
    topic = Topic[InboundChat]("/inbound/chat", backpressure_policy="drop-newest")
    q = asyncio.Queue(maxsize=2)
    topic.add_subscriber("node-a", q)

    msgs = [_chat_msg(str(i)) for i in range(3)]
    topic.put(msgs[0])
    topic.put(msgs[1])
    events = topic.put(msgs[2])

    assert len(events) == 1
    ev = events[0]
    assert ev.policy == "drop-newest"
    assert ev.dropped_message_id == msgs[2].id
    assert q.qsize() == 2


def test_backpressure_drop_newest_queue_retains_older_messages():
    topic = Topic[InboundChat]("/inbound/chat", backpressure_policy="drop-newest")
    q = asyncio.Queue(maxsize=2)
    topic.add_subscriber("n", q)

    msgs = [_chat_msg(str(i)) for i in range(3)]
    topic.put(msgs[0])
    topic.put(msgs[1])
    topic.put(msgs[2])

    remaining = [q.get_nowait(), q.get_nowait()]
    assert remaining[0].payload.text == "0"
    assert remaining[1].payload.text == "1"


def test_backpressure_event_includes_queue_size():
    topic = Topic[InboundChat]("/inbound/chat")
    q = asyncio.Queue(maxsize=1)
    topic.add_subscriber("n", q)

    topic.put(_chat_msg("0"))
    events = topic.put(_chat_msg("1"))

    assert events[0].queue_size == 1


def test_backpressure_only_for_full_queues():
    topic = Topic[InboundChat]("/inbound/chat")
    q1 = asyncio.Queue(maxsize=10)  # plenty of space
    q2 = asyncio.Queue(maxsize=1)  # will fill
    topic.add_subscriber("spacious", q1)
    topic.add_subscriber("tight", q2)

    topic.put(_chat_msg("0"))
    events = topic.put(_chat_msg("1"))  # only q2 triggers backpressure

    assert len(events) == 1
    assert events[0].subscriber_node == "tight"


# ── wildcard matching ─────────────────────────────────────────────────────────


def test_matches_exact():
    topic = Topic[InboundChat]("/inbound/chat")
    assert topic.matches("/inbound/chat") is True
    assert topic.matches("/inbound/other") is False


def test_matches_single_wildcard_one_segment():
    topic = Topic[InboundChat]("/system/lifecycle")
    assert topic.matches("/system/*") is True


def test_matches_single_wildcard_does_not_cross_segments():
    topic = Topic[InboundChat]("/system/deep/topic")
    assert topic.matches("/system/*") is False


def test_matches_double_wildcard_multiple_segments():
    topic = Topic[InboundChat]("/system/a/b/c")
    assert topic.matches("/system/**") is True


def test_matches_double_wildcard_zero_segments():
    topic = Topic[InboundChat]("/system")
    assert topic.matches("/system/**") is True


def test_matches_double_wildcard_root():
    topic = Topic[InboundChat]("/system/lifecycle")
    assert topic.matches("/**") is True


def test_matches_no_false_positive():
    topic = Topic[InboundChat]("/tools/request")
    assert topic.matches("/tools/result") is False
    assert topic.matches("/other/**") is False
