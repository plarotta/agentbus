"""Layer 1 — Contracts.

The wire envelope plus the per-topic payload schema registry. This is the only
module the whole stack depends on: nodes, the bus core, transports, and remote
clients all import from here. Everything else points its dependency arrows at
this file.

Two representations of a message coexist by design:

- ``Message[T]`` (``message.py``) — the *in-process* envelope. ``payload`` is a
  live, typed Pydantic model instance. This is what nodes publish and receive.
- ``Envelope`` (here) — the *wire* form. ``payload`` is a plain ``dict``. This is
  what crosses a process boundary (websocket/JSON) in the transport layer.

``Envelope.from_message`` / ``Envelope.to_message`` convert between them, and
``to_message`` re-validates the payload dict against ``TOPIC_SCHEMAS`` — so a
buggy or rogue remote client cannot inject a payload that violates the topic's
contract (defense in depth: the publisher validated at the call site, the
receiver validates again on the way in).
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentbus.errors import TopicSchemaError

if TYPE_CHECKING:
    from agentbus.message import Message


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Topic schema registry ──────────────────────────────────────────────────
#
# Maps a concrete topic name → the Pydantic model that validates its payload.
# This is the cross-process contract: a remote client cannot share a ``Topic[T]``
# *object*, but it can import this dict (or be told its contents). ``Topic[T]``
# populates it as a registration side effect, so wiring a bus also freezes the
# contract that transports and the daemon validate against.
TOPIC_SCHEMAS: dict[str, type[BaseModel]] = {}


def register_schema(
    topic: str, model: type[BaseModel], *, registry: dict[str, type[BaseModel]] | None = None
) -> None:
    """Bind ``topic`` to ``model`` in ``registry`` (default: the global one).

    Idempotent for the same model. Re-binding a topic to a *different* model is
    allowed (last writer wins) because the registry is module-global and many
    short-lived buses in a test run legitimately re-register the same names.
    """
    reg = TOPIC_SCHEMAS if registry is None else registry
    reg[topic] = model


def schema_for(
    topic: str, *, registry: dict[str, type[BaseModel]] | None = None
) -> type[BaseModel] | None:
    """Return the model bound to ``topic``, or ``None`` if unregistered."""
    reg = TOPIC_SCHEMAS if registry is None else registry
    return reg.get(topic)


def validate_payload(
    topic: str, payload: dict, *, registry: dict[str, type[BaseModel]] | None = None
) -> BaseModel:
    """Validate a raw payload dict against the topic's registered schema.

    Returns the hydrated model instance. Raises ``TopicSchemaError`` if the
    topic has no registered schema or the payload fails validation — this is the
    chokepoint a daemon runs on every inbound wire envelope.
    """
    model = schema_for(topic, registry=registry)
    if model is None:
        raise TopicSchemaError(f"Topic {topic!r} has no registered schema")
    try:
        return model.model_validate(payload)
    except Exception as e:  # pydantic ValidationError, etc.
        raise TopicSchemaError(f"Payload for topic {topic!r} failed validation: {e}") from e


class Envelope(BaseModel):
    """Serializable wire form of a bus message.

    Field names mirror ``Message`` (``source_node``, ``timestamp``) so the two
    convert cleanly; ``payload`` is a plain dict and ``reply_to`` carries the
    request/reply rendezvous topic. Frozen so a delivered envelope can't be
    mutated out from under a subscriber.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=_utcnow)
    source_node: str
    topic: str
    correlation_id: str | None = None
    reply_to: str | None = None
    offset: int | None = None
    payload: dict

    @classmethod
    def from_message(cls, msg: "Message") -> "Envelope":
        """Serialize an in-process ``Message`` to a wire ``Envelope``."""
        payload = msg.payload
        payload_dict = payload.model_dump() if isinstance(payload, BaseModel) else dict(payload)
        return cls(
            id=msg.id,
            timestamp=msg.timestamp,
            source_node=msg.source_node,
            topic=msg.topic,
            correlation_id=msg.correlation_id,
            reply_to=msg.reply_to,
            offset=msg.offset,
            payload=payload_dict,
        )

    def to_message(self, *, registry: dict[str, type[BaseModel]] | None = None) -> "Message":
        """Hydrate this wire envelope into a typed ``Message``.

        Re-validates ``payload`` against ``TOPIC_SCHEMAS`` — raises
        ``TopicSchemaError`` if the topic is unregistered or the payload is
        invalid. This is the receive-side defense for transports.
        """
        from agentbus.message import Message

        model = validate_payload(self.topic, self.payload, registry=registry)
        return Message(
            id=self.id,
            timestamp=self.timestamp,
            source_node=self.source_node,
            topic=self.topic,
            correlation_id=self.correlation_id,
            reply_to=self.reply_to,
            offset=self.offset,
            payload=model,
        )
