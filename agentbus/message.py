from datetime import UTC, datetime
from typing import Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Message(BaseModel, Generic[T]):
    """Immutable envelope wrapping every payload on the bus.

    Nodes never construct Message objects directly. They pass payloads to
    BusHandle.publish(), and the bus builds the envelope — setting id,
    timestamp, source_node, and topic at creation time. Because the model
    is frozen, source_node integrity is guaranteed from the moment of creation.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=_utcnow)
    source_node: str  # set by the bus at publish time, never by the node
    topic: str
    correlation_id: str | None = None
    payload: T
