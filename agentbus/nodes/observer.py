import logging

from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.system import BackpressureEvent, LifecycleEvent, TelemetryEvent

logger = logging.getLogger(__name__)


class ObserverNode(Node):
    """Read-only observer for system topics."""

    name = "observer"
    subscriptions = ["/system/*"]
    publications = []

    def __init__(self) -> None:
        self.events: list[Message] = []

    async def on_message(self, msg: Message) -> None:
        self.events.append(msg)
        payload = msg.payload
        if isinstance(payload, LifecycleEvent):
            logger.info("Lifecycle event: node=%s event=%s", payload.node, payload.event)
        elif isinstance(payload, BackpressureEvent):
            logger.warning(
                "Backpressure on %s for %s (%s)",
                payload.topic,
                payload.subscriber_node,
                payload.policy,
            )
        elif isinstance(payload, TelemetryEvent):
            logger.debug("Telemetry event: %s", payload.event)
