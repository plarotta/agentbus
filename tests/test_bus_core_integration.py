"""MessageBus ↔ BusCore integration: hooks and transport sinks on a live bus."""

from pydantic import BaseModel

from agentbus.core import CallbackSink
from agentbus.message import Message
from agentbus.node import Node
from agentbus.topic import Topic


class _P(BaseModel):
    n: int


class _Emitter(Node):
    name = "emitter"
    publications = ["/t"]

    async def on_init(self, bus):
        self._bus = bus

    async def on_shutdown(self):
        await self._bus.publish("/t", _P(n=1))


class _Collector(Node):
    name = "collector"
    subscriptions = ["/t"]

    def __init__(self):
        self.seen: list[int] = []

    async def on_message(self, msg: Message):
        self.seen.append(msg.payload.n)


def _make_bus():
    from agentbus.bus import MessageBus

    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[_P]("/t", retention=10))
    return bus


async def test_hook_blocks_delivery_to_nodes():
    bus = _make_bus()
    collector = _Collector()
    bus.register_node(collector)
    bus.add_hook(lambda m: None if m.topic == "/t" else m)

    bus.publish("/t", _P(n=5))
    await bus.spin_once(timeout=0.2)
    assert collector.seen == []  # hook blocked it before node fan-out
    assert len(bus.history("/t")) == 0  # and before the retention buffer


async def test_hook_transforms_payload_seen_by_node():
    bus = _make_bus()
    collector = _Collector()
    bus.register_node(collector)
    bus.add_hook(lambda m: m.model_copy(update={"payload": _P(n=m.payload.n * 10)}))

    bus.publish("/t", _P(n=4))
    await bus.spin_once(timeout=0.2)
    assert collector.seen == [40]


async def test_remove_hook_restores_delivery():
    bus = _make_bus()
    collector = _Collector()
    bus.register_node(collector)
    remove = bus.add_hook(lambda m: None)
    remove()

    bus.publish("/t", _P(n=7))
    await bus.spin_once(timeout=0.2)
    assert collector.seen == [7]


async def test_transport_sink_receives_via_subscribe():
    bus = _make_bus()
    bus.register_node(_Collector())
    tapped: list[Message] = []
    bus.subscribe("/**", CallbackSink(tapped.append))

    bus.publish("/t", _P(n=9))
    await bus.spin_once(timeout=0.2)
    assert [m.payload.n for m in tapped] == [9]


async def test_message_log_is_core_log():
    bus = _make_bus()
    bus.register_node(_Collector())
    bus.publish("/t", _P(n=1))
    assert bus._message_log is bus._core.log
    assert any(m.topic == "/t" for m in bus._message_log)
