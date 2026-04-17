from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from agentbus.bus import MessageBus
from agentbus.gateway import GatewayNode
from agentbus.message import Message
from agentbus.node import BusHandle, Node
from agentbus.nodes.observer import ObserverNode
from agentbus.topic import Topic

try:
    __version__ = _pkg_version("agentbus")
except PackageNotFoundError:  # editable install before build
    __version__ = "0.0.0+unknown"

__all__ = [
    "BusHandle",
    "GatewayNode",
    "Message",
    "MessageBus",
    "Node",
    "ObserverNode",
    "Topic",
    "__version__",
]
