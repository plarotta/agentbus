"""
Sensor monitoring pipeline — multi-stage stream processing without an LLM.

Shows how AgentBus routes typed messages through a stateful processing chain:

    SensorNode → /readings → StatsNode → /stats → AlertNode → /alerts
                                                              ↓
                                                         DisplayNode

Key concepts demonstrated:
  - Typed pub/sub schemas with no LLM dependency
  - Stateful nodes (rolling window in StatsNode)
  - Conditional publishing (AlertNode only fires on level change)
  - Retention buffer: bus.history() for post-run analysis

Run:
    uv run python examples/sensor_pipeline/main.py
"""

import asyncio
import random
import statistics
from collections import deque

from agentbus import MessageBus, Node, Topic
from agentbus.message import Message
from pydantic import BaseModel

NUM_READINGS = 60
WINDOW_SIZE = 10
WARNING_THRESHOLD = 74.0
CRITICAL_THRESHOLD = 82.0


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SensorReading(BaseModel):
    sensor_id: str
    value: float


class WindowStats(BaseModel):
    sensor_id: str
    mean: float
    stdev: float
    p_min: float
    p_max: float


class Alert(BaseModel):
    sensor_id: str
    level: str          # "warning" | "critical" | "resolved"
    mean: float


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

class SensorNode(Node):
    """Emits simulated temperature readings with a drift spike after midpoint."""

    name = "sensor"
    subscriptions = []
    publications = ["/readings"]

    def __init__(self) -> None:
        self._bus = None

    async def on_init(self, bus) -> None:
        self._bus = bus
        asyncio.create_task(self._emit())

    async def _emit(self) -> None:
        for i in range(NUM_READINGS):
            # Normal operating range, then a temperature spike in the second half
            base = 80.0 if i > NUM_READINGS // 2 else 65.0
            value = round(random.gauss(base, 4.0), 2)
            await self._bus.publish(
                "/readings",
                SensorReading(sensor_id="sensor-A", value=value),
            )
            await asyncio.sleep(0.02)


class StatsNode(Node):
    """Maintains a rolling window per sensor and publishes statistics."""

    name = "stats"
    subscriptions = ["/readings"]
    publications = ["/stats"]

    def __init__(self) -> None:
        self._bus = None
        self._windows: dict[str, deque] = {}

    async def on_init(self, bus) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        r: SensorReading = msg.payload
        window = self._windows.setdefault(r.sensor_id, deque(maxlen=WINDOW_SIZE))
        window.append(r.value)
        if len(window) < 3:
            return
        data = list(window)
        await self._bus.publish(
            "/stats",
            WindowStats(
                sensor_id=r.sensor_id,
                mean=round(statistics.mean(data), 2),
                stdev=round(statistics.stdev(data), 2),
                p_min=round(min(data), 2),
                p_max=round(max(data), 2),
            ),
        )


class AlertNode(Node):
    """Publishes an alert only when the severity level changes."""

    name = "alerter"
    subscriptions = ["/stats"]
    publications = ["/alerts"]

    def __init__(self) -> None:
        self._bus = None
        self._prev_level: dict[str, str] = {}

    async def on_init(self, bus) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        s: WindowStats = msg.payload
        if s.mean >= CRITICAL_THRESHOLD:
            level = "critical"
        elif s.mean >= WARNING_THRESHOLD:
            level = "warning"
        else:
            level = "ok"

        prev = self._prev_level.get(s.sensor_id, "ok")
        if level == prev:
            return

        self._prev_level[s.sensor_id] = level
        if level != "ok":
            await self._bus.publish(
                "/alerts",
                Alert(sensor_id=s.sensor_id, level=level, mean=s.mean),
            )
        else:
            await self._bus.publish(
                "/alerts",
                Alert(sensor_id=s.sensor_id, level="resolved", mean=s.mean),
            )


class DisplayNode(Node):
    """Prints a progress line every N stats updates and all alerts immediately."""

    name = "display"
    subscriptions = ["/stats", "/alerts"]
    publications = []

    def __init__(self) -> None:
        self._tick = 0

    async def on_message(self, msg: Message) -> None:
        if isinstance(msg.payload, WindowStats):
            s: WindowStats = msg.payload
            self._tick += 1
            if self._tick % 8 == 0:
                bar = _bar(s.mean, low=55.0, high=95.0, width=20)
                print(f"  stats   {bar}  mean={s.mean:5.1f}  stdev={s.stdev:.2f}")
        elif isinstance(msg.payload, Alert):
            a: Alert = msg.payload
            tag = {"warning": "[ WARN ]", "critical": "[CRIT  ]", "resolved": "[ OK   ]"}[a.level]
            print(f"  {tag}  {a.sensor_id}  mean={a.mean:.1f}")


def _bar(value: float, low: float, high: float, width: int) -> str:
    """Simple ASCII bar chart."""
    fraction = max(0.0, min(1.0, (value - low) / (high - low)))
    filled = round(fraction * width)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[SensorReading]("/readings", retention=NUM_READINGS))
    bus.register_topic(Topic[WindowStats]("/stats", retention=20))
    bus.register_topic(Topic[Alert]("/alerts", retention=50))

    bus.register_node(SensorNode())
    bus.register_node(StatsNode())
    bus.register_node(AlertNode())
    bus.register_node(DisplayNode())

    print(f"Streaming {NUM_READINGS} readings  (warn >={WARNING_THRESHOLD}  crit >={CRITICAL_THRESHOLD})\n")

    await bus.spin(
        until=lambda: len(bus.history("/readings", NUM_READINGS + 1)) >= NUM_READINGS,
        timeout=30.0,
    )

    # Post-run summary from retention buffers
    readings = bus.history("/readings", NUM_READINGS)
    values = [r.payload.value for r in readings]
    alerts = bus.history("/alerts", 50)

    print(f"\n{'─' * 50}")
    print(f"  readings  : {len(readings)}")
    print(f"  temp range: {min(values):.1f} – {max(values):.1f}  (mean {statistics.mean(values):.1f})")
    print(f"  alerts    : {len(alerts)}")
    for a in alerts:
        print(f"    {a.payload.level:9s}  at mean={a.payload.mean:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
