import asyncio

from agentbus import GatewayNode, MessageBus, Node, ObserverNode, Topic
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


class Recorder(Node):
    name = "recorder"
    subscriptions = ["/outbound"]
    publications = []

    def __init__(self) -> None:
        self.seen: list[OutboundChat] = []

    async def on_message(self, msg: Message) -> None:
        self.seen.append(msg.payload)


class DemoGateway(GatewayNode):
    name = "gateway"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def _listen_external(self) -> None:
        await self.publish_external(
            InboundChat(channel="gateway", sender="outside", text="hello"),
        )
        while True:
            await asyncio.sleep(1)

    async def _send_external(self, msg: Message) -> None:
        self.sent.append(msg.payload.text)


async def test_echo_agent_end_to_end():
    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[InboundChat]("/inbound", retention=10))
    bus.register_topic(Topic[OutboundChat]("/outbound", retention=10))
    recorder = Recorder()
    bus.register_node(EchoNode())
    bus.register_node(recorder)

    async def publish_messages():
        await asyncio.sleep(0.05)
        for text in ("abc", "def", "ghi"):
            bus.publish("/inbound", InboundChat(channel="cli", sender="user", text=text))

    asyncio.create_task(publish_messages())
    await bus.spin(timeout=0.3)

    assert [message.text for message in recorder.seen] == ["cba", "fed", "ihg"]


async def test_observer_node_receives_lifecycle_events():
    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[InboundChat]("/inbound"))
    observer = ObserverNode()
    bus.register_node(observer)

    await bus.spin(timeout=0.05)

    assert any(msg.payload.event == "started" for msg in observer.events)


async def test_gateway_node_subclass_can_listen_and_send():
    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[InboundChat]("/inbound", retention=10))
    bus.register_topic(Topic[OutboundChat]("/outbound", retention=10))
    gateway = DemoGateway()
    recorder = Recorder()
    bus.register_node(gateway)
    bus.register_node(EchoNode())
    bus.register_node(recorder)

    await bus.spin(timeout=0.2)

    assert recorder.seen
    assert gateway.sent == ["olleh"]
