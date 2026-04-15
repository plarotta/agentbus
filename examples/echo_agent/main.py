import asyncio

from agentbus import MessageBus, Node, ObserverNode, Topic
from agentbus.message import Message
from agentbus.schemas.common import InboundChat, OutboundChat


class EchoNode(Node):
    name = "echo"
    subscriptions = ["/inbound"]
    publications = ["/outbound"]

    def __init__(self) -> None:
        self._bus = None

    async def on_init(self, bus) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        await self._bus.publish(  # type: ignore[union-attr]
            "/outbound",
            OutboundChat(text=msg.payload.text[::-1], reply_to=msg.source_node),
        )


async def main() -> None:
    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[InboundChat]("/inbound", retention=10))
    bus.register_topic(Topic[OutboundChat]("/outbound", retention=10))
    bus.register_node(EchoNode())
    bus.register_node(ObserverNode())

    async def seed_messages() -> None:
        await asyncio.sleep(0.05)
        for text in ("hello", "agentbus", "echo"):
            bus.publish("/inbound", InboundChat(channel="demo", sender="user", text=text))

    asyncio.create_task(seed_messages())
    await bus.spin(until=lambda: len(bus.history("/outbound", 10)) >= 3)


if __name__ == "__main__":
    asyncio.run(main())
