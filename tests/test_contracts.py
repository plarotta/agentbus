"""Tests for Layer 1 — contracts (Envelope + TOPIC_SCHEMAS registry)."""

import pytest
from pydantic import BaseModel

from agentbus.contracts import (
    Envelope,
    register_schema,
    schema_for,
    validate_payload,
)
from agentbus.errors import TopicSchemaError
from agentbus.message import Message
from agentbus.topic import Topic


class _Payload(BaseModel):
    value: int
    label: str = "x"


class _Other(BaseModel):
    note: str


def test_register_and_lookup_uses_isolated_registry():
    reg: dict[str, type[BaseModel]] = {}
    register_schema("/a", _Payload, registry=reg)
    assert schema_for("/a", registry=reg) is _Payload
    assert schema_for("/missing", registry=reg) is None


def test_topic_construction_registers_schema_globally():
    Topic[_Payload]("/contracts/topic-reg")
    assert schema_for("/contracts/topic-reg") is _Payload


def test_validate_payload_hydrates_model():
    reg: dict[str, type[BaseModel]] = {}
    register_schema("/a", _Payload, registry=reg)
    model = validate_payload("/a", {"value": 7}, registry=reg)
    assert isinstance(model, _Payload)
    assert model.value == 7
    assert model.label == "x"


def test_validate_payload_unregistered_topic_raises():
    with pytest.raises(TopicSchemaError, match="no registered schema"):
        validate_payload("/nope", {"value": 1}, registry={})


def test_validate_payload_invalid_payload_raises():
    reg: dict[str, type[BaseModel]] = {}
    register_schema("/a", _Payload, registry=reg)
    with pytest.raises(TopicSchemaError, match="failed validation"):
        validate_payload("/a", {"label": "missing-value"}, registry=reg)


def test_envelope_from_message_serializes_payload_to_dict():
    msg = Message(source_node="n", topic="/a", payload=_Payload(value=3, label="y"))
    env = Envelope.from_message(msg)
    assert env.payload == {"value": 3, "label": "y"}
    assert env.source_node == "n"
    assert env.topic == "/a"
    assert env.id == msg.id
    assert env.reply_to is None


def test_envelope_round_trip_preserves_identity_fields():
    reg: dict[str, type[BaseModel]] = {}
    register_schema("/a", _Payload, registry=reg)
    original = Message(
        source_node="n",
        topic="/a",
        correlation_id="cid-1",
        reply_to="/replies",
        payload=_Payload(value=42),
    )
    env = Envelope.from_message(original)
    restored = env.to_message(registry=reg)
    assert restored.id == original.id
    assert restored.correlation_id == "cid-1"
    assert restored.reply_to == "/replies"
    assert restored.source_node == "n"
    assert restored.payload == original.payload


def test_envelope_to_message_revalidates_against_registry():
    reg: dict[str, type[BaseModel]] = {}
    register_schema("/a", _Payload, registry=reg)
    # A rogue client hand-builds an envelope with a garbage payload.
    env = Envelope(source_node="rogue", topic="/a", payload={"value": "not-an-int"})
    with pytest.raises(TopicSchemaError):
        env.to_message(registry=reg)


def test_envelope_to_message_unregistered_topic_raises():
    env = Envelope(source_node="n", topic="/unknown", payload={})
    with pytest.raises(TopicSchemaError, match="no registered schema"):
        env.to_message(registry={})


def test_envelope_is_frozen():
    env = Envelope(source_node="n", topic="/a", payload={"value": 1})
    with pytest.raises(Exception):
        env.topic = "/b"  # type: ignore[misc]
