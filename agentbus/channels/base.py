"""Channel plugin contract.

Every multi-channel adapter (Slack, Telegram, …) subclasses
:class:`ChannelPlugin` and exposes three pieces:

1. ``name`` — channel identifier used in ``agentbus.yaml`` and as the
   ``channel_name`` on the resulting :class:`~agentbus.gateway.GatewayNode`.
2. ``ConfigModel`` — a pydantic model for that channel's YAML section.
   The loader validates the YAML block against this model before the
   bus starts, so bad tokens or missing fields fail fast with a clear
   error rather than mid-conversation.
3. ``create_gateway(config)`` — returns a configured
   :class:`~agentbus.gateway.GatewayNode` instance.

Plugins also optionally expose a ``setup_wizard`` classmethod that
prompts the user for credentials and returns a ``ConfigModel`` instance.
The ``agentbus channels setup <name>`` CLI subcommand dispatches through
this hook. Missing wizards just mean "edit ``agentbus.yaml`` by hand".

Gateways inherit ``/system/channels`` publication from the base so
:class:`ChannelStatus` updates are a single method call
(:meth:`GatewayNode.publish_channel_status`). The base also exposes a
shared :class:`~agentbus.utils.CircuitBreaker` constant — every
channel's listener loop wraps its transport in one so a dead token
can't burn CPU forever.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel

from agentbus.gateway import GatewayNode

MAX_CONSECUTIVE_GATEWAY_FAILURES = 5


class ChannelRuntimeError(RuntimeError):
    """Raised when channel loading or lifecycle fails in a way the caller
    should surface to the user (missing SDK, malformed config, auth fail).
    Kept distinct from generic ``RuntimeError`` so the loader can catch it
    without swallowing bugs.
    """


ProbeStatus = Literal["ok", "warn", "fail"]


@dataclass
class ProbeResult:
    """Outcome of a lightweight per-channel liveness check.

    Populated by :meth:`ChannelPlugin.probe` and rendered by
    ``agentbus doctor``. Follows the same ``ok/warn/fail`` shape as
    the other doctor checks so they can be folded into a single report.
    """

    status: ProbeStatus
    detail: str = ""


class ChannelPlugin[ConfigT: BaseModel](ABC):
    """Abstract contract implemented by every channel adapter.

    Parameterized over the concrete :class:`pydantic.BaseModel` config
    type so subclasses can narrow ``create_gateway``'s argument type
    without tripping Liskov — e.g. ``class SlackPlugin(ChannelPlugin[SlackConfig])``.
    """

    name: ClassVar[str]
    ConfigModel: ClassVar[type[BaseModel]]

    @classmethod
    def setup_wizard(cls, existing: dict | None = None) -> BaseModel:  # pragma: no cover
        """Interactive first-run setup. Subclasses override. Default
        implementation raises — callers should catch and tell the user to
        edit ``agentbus.yaml`` by hand."""
        raise NotImplementedError(
            f"{cls.__name__} does not provide an interactive setup wizard; "
            "edit agentbus.yaml manually"
        )

    @classmethod
    async def probe(cls, config: ConfigT) -> ProbeResult:
        """Lightweight liveness check for ``agentbus doctor``.

        Default implementation returns ``warn`` — subclasses override
        with a cheap auth check (Slack ``auth.test``, Telegram
        ``getMe``, etc.). The probe must be safe to run from a cold
        process: no bus, no ``on_init``, no background tasks.
        """
        return ProbeResult(status="warn", detail="no probe implemented")

    @classmethod
    @abstractmethod
    def create_gateway(cls, config: ConfigT) -> GatewayNode:
        """Instantiate a ``GatewayNode`` for this channel. The returned
        node must set ``channel_name`` equal to the plugin's ``name``
        (inherited handling lives in each gateway's ``__init__``)."""
