from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from agentbus.bus import MessageBus
from agentbus.client import BusClient
from agentbus.contracts import (
    TOPIC_SCHEMAS,
    Envelope,
    register_schema,
    schema_for,
    validate_payload,
)
from agentbus.core import (
    BusCore,
    CallbackSink,
    InMemoryLog,
    LogStore,
    QueueSink,
    Sink,
    SinkClosed,
)
from agentbus.gateway import GatewayNode
from agentbus.journal import SqliteLog
from agentbus.logging_config import node_logger, setup_logging
from agentbus.message import Message
from agentbus.node import BusHandle, Node
from agentbus.nodes.observer import ObserverNode
from agentbus.server import BusServer
from agentbus.topic import Topic
from agentbus.transport import (
    InMemoryTransport,
    LocalTarget,
    Transport,
    WebSocketTransport,
)

try:
    __version__ = _pkg_version("agentbus")
except PackageNotFoundError:  # editable install before build
    __version__ = "0.0.0+unknown"

__all__ = [
    "TOPIC_SCHEMAS",
    "BusClient",
    "BusCore",
    "BusHandle",
    "BusServer",
    "CallbackSink",
    "Envelope",
    "GatewayNode",
    "InMemoryLog",
    "InMemoryTransport",
    "LocalTarget",
    "LogStore",
    "Message",
    "MessageBus",
    "Node",
    "ObserverNode",
    "QueueSink",
    "Sink",
    "SinkClosed",
    "SqliteLog",
    "Topic",
    "Transport",
    "WebSocketTransport",
    "__version__",
    "node_logger",
    "register_schema",
    "schema_for",
    "setup_logging",
    "validate_payload",
]
