"""Multi-channel gateway package.

Each channel lives in its own subpackage (``slack/``, ``telegram/``) and
registers a :class:`ChannelPlugin`. The :mod:`agentbus.channels.loader`
module reads the ``channels:`` section of ``agentbus.yaml``, imports the
requested plugins, and builds one :class:`~agentbus.gateway.GatewayNode`
per enabled channel.

Gateways are optional runtime dependencies — importing
``agentbus.channels`` never triggers an import of slack-bolt, httpx, or
any other channel SDK. The subpackages do that lazily when a channel is
actually enabled.
"""

from .base import (
    MAX_CONSECUTIVE_GATEWAY_FAILURES,
    ChannelPlugin,
    ChannelRuntimeError,
)
from .loader import (
    ChannelsRuntime,
    load_channels_from_dict,
    open_channels_runtime,
    register_plugin,
    registered_plugins,
)

__all__ = [
    "MAX_CONSECUTIVE_GATEWAY_FAILURES",
    "ChannelPlugin",
    "ChannelRuntimeError",
    "ChannelsRuntime",
    "load_channels_from_dict",
    "open_channels_runtime",
    "register_plugin",
    "registered_plugins",
]
