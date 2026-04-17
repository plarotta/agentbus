import asyncio
import enum
from abc import ABC
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from agentbus.message import Message
from agentbus.utils import CircuitBreaker

_DEFAULT_MAX_ERRORS = 10


class NodeState(enum.Enum):
    """Lifecycle state of a node as tracked by the bus."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@runtime_checkable
class BusHandle(Protocol):
    """Capability interface given to nodes in on_init().

    Defined here (not bus.py) to avoid circular imports. The concrete
    _BusHandle implementation lives in bus.py and satisfies this protocol
    structurally — no explicit registration needed.
    """

    async def publish(self, topic: str, payload: Any, correlation_id: str | None = None) -> None:
        """Publish a payload to a declared topic."""
        ...

    async def request(
        self,
        topic: str,
        payload: Any,
        reply_on: str,
        *,
        timeout: float = 30.0,
    ) -> Message:
        """Publish a payload and await a correlated reply on reply_on."""
        ...

    async def topic_history(self, topic: str, n: int = 10) -> list[Message]:
        """Return the last n messages from a topic's retention buffer."""
        ...


class Node(ABC):
    """Abstract base class for all AgentBus nodes.

    Declare class-level attributes to describe the node's interface. Override
    the lifecycle hooks you need — all default to no-ops.

    Example:
        class PlannerNode(Node):
            name = "planner"
            subscriptions = ["/inbound/chat"]
            publications = ["/tools/request"]
            concurrency_mode = "serial"

            async def on_message(self, msg: Message) -> None:
                ...
    """

    name: ClassVar[str]
    subscriptions: ClassVar[list[str]] = []
    publications: ClassVar[list[str]] = []
    concurrency: ClassVar[int] = 1
    concurrency_mode: ClassVar[Literal["parallel", "serial"]] = "parallel"

    async def on_init(self, bus: BusHandle) -> None:
        """Called once before the node begins receiving messages."""

    async def on_message(self, msg: Message) -> None:
        """Called for each message delivered to this node."""

    async def on_shutdown(self) -> None:
        """Called when the bus begins shutdown."""


_DEFAULT_QUEUE_SIZE = 100


class NodeHandle:
    """Runtime wrapper around a Node instance, managed by the bus.

    Holds the message delivery queue, the concurrency semaphore, and per-node
    metrics. The bus creates one NodeHandle per registered node. Tests can
    construct NodeHandles directly without a MessageBus instance.

    Serial mode always uses Semaphore(1) regardless of node.concurrency.
    """

    def __init__(
        self,
        node: Node,
        *,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        max_errors: int = _DEFAULT_MAX_ERRORS,
    ) -> None:
        self.node = node
        self.state = NodeState.CREATED
        self.queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=queue_size)
        if node.concurrency_mode == "serial":
            self.semaphore = asyncio.Semaphore(1)
        else:
            self.semaphore = asyncio.Semaphore(max(1, node.concurrency))
        # Per-node metrics (updated by the bus during spin)
        self.messages_received: int = 0
        self.messages_published: int = 0
        self.errors: int = 0
        self.error_breaker = CircuitBreaker(name=f"node:{node.name}", max_failures=max_errors)
