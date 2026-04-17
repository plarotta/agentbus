"""Bus integration test: correlation ID is exposed to logs inside on_message."""

from __future__ import annotations

import io
import json
import logging

import pytest

from agentbus.bus import MessageBus
from agentbus.logging_config import setup_logging
from agentbus.message import Message
from agentbus.node import BusHandle, Node
from agentbus.schemas.common import InboundChat
from agentbus.topic import Topic


@pytest.fixture(autouse=True)
def _reset_agentbus_logger():
    yield
    logger = logging.getLogger("agentbus")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


async def test_on_message_records_are_tagged_with_correlation_id():
    stream = io.StringIO()
    setup_logging(level="DEBUG", format="json", stream=stream)

    seen: list[str | None] = []

    class Sink(Node):
        name = "sink"
        subscriptions = ["/inbound"]

        async def on_init(self, b: BusHandle) -> None:
            self._log = self.logger  # child: agentbus.node.sink

        async def on_message(self, msg: Message) -> None:
            self._log.info("handled %s", msg.payload.text)
            seen.append(msg.correlation_id)

    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[InboundChat]("/inbound"))
    bus.register_node(Sink())

    # Publish with an explicit correlation ID and drain one message.
    bus.publish(
        "/inbound",
        InboundChat(channel="test", sender="u", text="ping"),
        correlation_id="cid-7",
    )
    await bus._init_phase()
    await bus.spin_once(timeout=1.0)

    assert seen == ["cid-7"]

    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    sink_records = [r for r in records if r.get("logger") == "agentbus.node.sink"]
    assert sink_records, f"no sink records in {records}"
    assert sink_records[-1]["msg"] == "handled ping"
    assert sink_records[-1]["correlation_id"] == "cid-7"


async def test_correlation_id_resets_between_dispatches():
    """Records logged outside any dispatch should not carry a stale CID."""

    stream = io.StringIO()
    setup_logging(level="DEBUG", format="json", stream=stream)

    class Sink(Node):
        name = "sink"
        subscriptions = ["/inbound"]

        async def on_message(self, msg: Message) -> None:
            self.logger.info("inside")

    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[InboundChat]("/inbound"))
    bus.register_node(Sink())
    bus.publish(
        "/inbound",
        InboundChat(channel="t", sender="u", text="x"),
        correlation_id="cid-inner",
    )
    await bus._init_phase()
    await bus.spin_once(timeout=1.0)

    # After dispatch returns, a log line from outside should have no CID.
    logging.getLogger("agentbus.test").info("outside")

    records = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    outside = next(r for r in records if r["logger"] == "agentbus.test")
    assert "correlation_id" not in outside
