import asyncio
from collections import deque
from typing import Any, Generic, Literal, TypeVar

from agentbus.errors import TopicSchemaError
from agentbus.message import Message
from agentbus.schemas.system import BackpressureEvent

T = TypeVar("T")


class Topic(Generic[T]):
    """Typed pub/sub topic with fan-out, retention, and backpressure.

    Must be parameterized with a schema type before instantiation:

        topic = Topic[ToolRequest]("/tools/request", retention=10)

    Wildcard patterns (used with matches()):
        *   matches exactly one path segment
        **  matches zero or more path segments
    """

    _schema: type  # set per-subclass by __class_getitem__

    def __class_getitem__(cls, schema_type: type) -> type["Topic"]:
        # Runtime override: returns a concrete subclass bound to schema_type, so
        # `Topic[X]("/t")` produces an instance whose validate_payload checks X.
        # mypy's Generic[T] machinery handles the static side; this override
        # intentionally diverges at runtime.
        return type(
            f"Topic[{schema_type.__name__}]",
            (cls,),
            {"_schema": schema_type},
        )

    def __init__(
        self,
        name: str,
        *,
        retention: int = 0,
        description: str = "",
        backpressure_policy: Literal["drop-oldest", "drop-newest"] = "drop-oldest",
    ) -> None:
        if not hasattr(self.__class__, "_schema"):
            raise TypeError(
                "Topic must be parameterized with a schema type: use Topic[SchemaType](name)"
            )
        self.name = name
        self.retention = retention
        self.description = description
        self.backpressure_policy = backpressure_policy
        self.schema: type = self.__class__._schema
        self._subscribers: dict[str, asyncio.Queue] = {}
        # maxlen=None → unbounded deque that we never append to when retention=0
        self._buffer: deque[Message] = deque(maxlen=retention if retention > 0 else None)

    def add_subscriber(self, node_name: str, queue: asyncio.Queue) -> None:
        """Register a node's queue to receive messages published to this topic."""
        self._subscribers[node_name] = queue

    def remove_subscriber(self, node_name: str) -> None:
        """Unregister a node from this topic. No-op if not subscribed."""
        self._subscribers.pop(node_name, None)

    def validate_payload(self, payload: Any) -> None:
        """Raise TopicSchemaError if payload is not an instance of the topic's schema."""
        if not isinstance(payload, self.schema):
            raise TopicSchemaError(
                f"Topic '{self.name}' expects {self.schema.__name__}, got {type(payload).__name__}"
            )

    def put(self, msg: Message) -> list[BackpressureEvent]:
        """Fan-out msg to all subscriber queues. Returns BackpressureEvents for any drops.

        Does not validate the payload — the bus calls validate_payload() before put().
        The retention buffer is updated unconditionally before fan-out.
        """
        if self.retention > 0:
            self._buffer.append(msg)

        events: list[BackpressureEvent] = []
        for node_name, queue in self._subscribers.items():
            events.extend(self._deliver(msg, node_name, queue))
        return events

    def _deliver(
        self, msg: Message, node_name: str, queue: asyncio.Queue
    ) -> list[BackpressureEvent]:
        try:
            queue.put_nowait(msg)
            return []
        except asyncio.QueueFull:
            pass

        if self.backpressure_policy == "drop-oldest":
            try:
                dropped = queue.get_nowait()
                queue.put_nowait(msg)
                return [
                    BackpressureEvent(
                        topic=self.name,
                        subscriber_node=node_name,
                        queue_size=queue.maxsize,
                        dropped_message_id=dropped.id,
                        policy="drop-oldest",
                    )
                ]
            except asyncio.QueueEmpty:
                # Raced to empty between the full-check and the get; just deliver.
                queue.put_nowait(msg)
                return []
        else:  # drop-newest
            return [
                BackpressureEvent(
                    topic=self.name,
                    subscriber_node=node_name,
                    queue_size=queue.maxsize,
                    dropped_message_id=msg.id,
                    policy="drop-newest",
                )
            ]

    def history(self, n: int | None = None) -> list[Message]:
        """Return messages from the retention buffer. Returns all if n is None."""
        if n is None:
            return list(self._buffer)
        return list(self._buffer)[-n:]

    def matches(self, pattern: str) -> bool:
        """Return True if this topic's name matches the subscription pattern.

        Wildcards:
            *   matches exactly one path segment
            **  matches zero or more path segments
        """
        return _match_pattern(pattern, self.name)


def _match_pattern(pattern: str, name: str) -> bool:
    """Match a topic name against a wildcard subscription pattern."""
    return _match_parts(pattern.split("/"), name.split("/"))


def _match_parts(p_parts: list[str], n_parts: list[str]) -> bool:
    if not p_parts and not n_parts:
        return True
    if not p_parts:
        return False
    if p_parts[0] == "**":
        # Consume zero or more name segments.
        return any(_match_parts(p_parts[1:], n_parts[i:]) for i in range(len(n_parts) + 1))
    if not n_parts:
        return False
    if p_parts[0] == "*" or p_parts[0] == n_parts[0]:
        return _match_parts(p_parts[1:], n_parts[1:])
    return False
