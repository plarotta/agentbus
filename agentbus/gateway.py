import asyncio
from abc import ABC, abstractmethod
from typing import Any

from agentbus.message import Message
from agentbus.node import BusHandle, Node


class GatewayNode(Node, ABC):
    """Base class for external-channel bridges."""

    subscriptions = ["/outbound"]
    publications = ["/inbound"]
    concurrency_mode = "serial"

    def __init__(self) -> None:
        self._bus: BusHandle | None = None
        self._listener_task: asyncio.Task | None = None

    async def on_init(self, bus: BusHandle) -> None:
        self._bus = bus
        self._listener_task = asyncio.create_task(self._listen_external())

    async def on_message(self, msg: Message) -> None:
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

    @abstractmethod
    async def _listen_external(self) -> None: ...

    @abstractmethod
    async def _send_external(self, msg: Message) -> None: ...
