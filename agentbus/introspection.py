from dataclasses import dataclass, field
from typing import Literal


@dataclass
class NodeStats:
    """Per-node message counters returned in SpinResult."""

    messages_received: int = 0
    messages_published: int = 0
    errors: int = 0


@dataclass
class SpinResult:
    """Summary returned by MessageBus.spin() after the bus stops."""

    messages_processed: int
    duration_s: float
    per_node: dict[str, NodeStats]
    errors: list[str] = field(default_factory=list)


@dataclass
class TopicInfo:
    """Snapshot of a topic's runtime state."""

    name: str
    schema_name: str
    retention: int
    subscriber_count: int
    message_count: int
    queue_depths: dict[str, int] = field(default_factory=dict)


@dataclass
class NodeInfo:
    """Snapshot of a node's runtime state."""

    name: str
    state: str
    concurrency: int
    concurrency_mode: str
    subscriptions: list[str]
    publications: list[str]
    messages_received: int = 0
    messages_published: int = 0
    errors: int = 0


@dataclass
class Edge:
    """A directed connection between a node and a topic."""

    node: str
    topic: str
    direction: Literal["sub", "pub"]


@dataclass
class BusGraph:
    """Full connectivity graph of the bus at a point in time."""

    nodes: list[NodeInfo]
    topics: list[TopicInfo]
    edges: list[Edge]
