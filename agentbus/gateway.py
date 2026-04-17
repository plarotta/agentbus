"""Base class for external-channel bridges (Slack, Telegram, …).

A ``GatewayNode`` subscribes to ``/outbound`` and publishes to ``/inbound``.
Concrete channel gateways set ``channel_name`` so messages with a mismatched
``OutboundChat.channel`` are silently ignored — this lets multiple gateways
coexist on the same bus without stepping on each other. When
``OutboundChat.channel`` is ``None`` the message is accepted by every
gateway, which matches single-channel deployments where the field is
optional.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

from agentbus.message import Message
from agentbus.node import BusHandle, Node
from agentbus.schemas.common import OutboundChat
from agentbus.schemas.system import ChannelStatus


class GatewayNode(Node, ABC):
    """Base class for external-channel bridges."""

    subscriptions = ["/outbound"]
    publications = ["/inbound", "/system/channels"]
    concurrency_mode = "serial"

    # Subclasses set this to filter /outbound by OutboundChat.channel.
    # None means "accept every outbound message" (legacy single-channel mode).
    channel_name: ClassVar[str | None] = None

    def __init__(self) -> None:
        self._bus: BusHandle | None = None
        self._listener_task: asyncio.Task | None = None

    async def on_init(self, bus: BusHandle) -> None:
        self._bus = bus
        self._listener_task = asyncio.create_task(self._listen_external())

    async def on_message(self, msg: Message) -> None:
        if isinstance(msg.payload, OutboundChat) and self.channel_name is not None:
            target = msg.payload.channel
            if target is not None and target != self.channel_name:
                return
        await self._send_external(msg)

    async def on_shutdown(self) -> None:
        if self._listener_task is None:
            return
        self._listener_task.cancel()
        await asyncio.gather(self._listener_task, return_exceptions=True)

    async def publish_external(self, payload: Any, *, topic: str = "/inbound") -> None:
        if self._bus is None:
            raise RuntimeError("GatewayNode is not initialized")
        await self._bus.publish(topic, payload)

    async def publish_channel_status(
        self,
        state: Literal["starting", "connected", "reconnecting", "error", "stopped"],
        *,
        detail: str | None = None,
    ) -> None:
        """Publish a :class:`ChannelStatus` update to ``/system/channels``.

        Requires ``channel_name`` to be set — gateways that subclass the
        base without filling in ``channel_name`` silently no-op here so
        legacy single-channel gateway tests don't need to opt in.
        """
        if self._bus is None or self.channel_name is None:
            return
        await self._bus.publish(
            "/system/channels",
            ChannelStatus(channel=self.channel_name, state=state, detail=detail),
        )

    @abstractmethod
    async def _listen_external(self) -> None: ...

    @abstractmethod
    async def _send_external(self, msg: Message) -> None: ...
