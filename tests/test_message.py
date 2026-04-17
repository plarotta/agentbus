from datetime import UTC

import pytest
from pydantic import ValidationError

from agentbus.message import Message
from agentbus.schemas.common import InboundChat


def _make_msg(**kwargs) -> Message:
    defaults = dict(
        source_node="bus",
        topic="/inbound",
        payload=InboundChat(channel="cli", sender="user", text="hello"),
    )
    return Message(**(defaults | kwargs))


def test_id_auto_generated():
    msg = _make_msg()
    assert msg.id
    assert len(msg.id) == 36  # uuid4 canonical form


def test_two_messages_have_different_ids():
    a = _make_msg()
    b = _make_msg()
    assert a.id != b.id


def test_timestamp_auto_generated():
    msg = _make_msg()
    assert msg.timestamp is not None


def test_timestamp_is_utc():
    msg = _make_msg()
    assert msg.timestamp.tzinfo is not None
    assert msg.timestamp.tzinfo == UTC


def test_message_frozen():
    msg = _make_msg()
    with pytest.raises((TypeError, ValidationError)):
        msg.source_node = "other"  # type: ignore[misc]


def test_source_node_required():
    with pytest.raises(ValidationError):
        Message(  # type: ignore[call-arg]
            topic="/inbound",
            payload=InboundChat(channel="cli", sender="user", text="hello"),
        )


def test_topic_required():
    with pytest.raises(ValidationError):
        Message(  # type: ignore[call-arg]
            source_node="bus",
            payload=InboundChat(channel="cli", sender="user", text="hello"),
        )


def test_correlation_id_defaults_none():
    msg = _make_msg()
    assert msg.correlation_id is None


def test_correlation_id_can_be_set():
    msg = _make_msg(correlation_id="abc-123")
    assert msg.correlation_id == "abc-123"


def test_payload_accessible():
    payload = InboundChat(channel="slack", sender="alice", text="hi")
    msg = _make_msg(payload=payload)
    assert msg.payload.text == "hi"
    assert msg.payload.sender == "alice"
