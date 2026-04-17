from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LifecycleEvent(BaseModel):
    """Published to /system/lifecycle on node state transitions.
    Auto-published by the bus — nodes do not publish this directly.
    """

    node: str
    event: Literal["started", "stopped", "error", "init_failed"]
    error: str | None = None
    timestamp: datetime = Field(default_factory=_utcnow)


class Heartbeat(BaseModel):
    """Published to /system/heartbeat on a fixed interval (default: 30s).
    Contains a snapshot of overall bus health.
    """

    uptime_s: float
    node_count: int
    topic_count: int
    total_messages: int
    messages_per_second: float  # rolling 60s window
    node_states: dict[str, str]  # {"planner": "RUNNING", ...}
    queue_depths: dict[str, int]  # {"planner": 0, "browser": 3}


class BackpressureEvent(BaseModel):
    """Published to /system/backpressure when a subscriber queue drops a message."""

    topic: str
    subscriber_node: str
    queue_size: int
    dropped_message_id: str
    policy: Literal["drop-oldest", "drop-newest"]


class TelemetryEvent(BaseModel):
    """Published to /system/telemetry by the harness for operational observability."""

    event: Literal[
        "stall_detected",  # user re-prompted without new info
        "context_pressure",  # >80% context window used
        "tool_timeout",  # tool exceeded timeout
        "model_demotion",  # fallback provider chain activated
        "compact_triggered",  # any compaction tier fired
        "breaker_tripped",  # any circuit breaker opened
    ]
    detail: str
    session_id: str
    timestamp: datetime = Field(default_factory=_utcnow)
