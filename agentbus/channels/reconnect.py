"""Reconnect / retry primitives for channel gateways.

The old gateway loops slept a hard-coded 5 seconds after every failure
and relied on :class:`~agentbus.utils.CircuitBreaker` alone to give up
after N failures. Two problems with that:

* **Thundering herd** — a whole fleet of gateways sharing one revoked
  token would all retry on the same cadence.
* **Blind retry** — no distinction between transient (``socket
  disconnected``, ``502``) and terminal (``token_revoked``,
  ``invalid_auth``) failures. The gateway would burn through the
  breaker's budget against an auth error that would never come back.

:class:`ReconnectPolicy` fixes the first with exponential backoff +
jitter; the gateways themselves fix the second by checking the
exception text against a channel-specific non-recoverable regex before
deciding to sleep-and-retry vs. publish-and-bail.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class ReconnectPolicy:
    """Exponential backoff with jitter.

    ``next_delay()`` returns the next sleep duration and advances the
    attempt counter. ``reset()`` is called after a successful
    reconnection cycle. ``exhausted`` goes True once ``max_attempts`` is
    reached — leave it ``None`` for unlimited (rely on
    :class:`~agentbus.utils.CircuitBreaker` for the hard stop).
    """

    initial_s: float = 2.0
    max_s: float = 30.0
    factor: float = 1.8
    jitter: float = 0.25
    max_attempts: int | None = None
    _attempt: int = field(default=0, init=False, repr=False)

    def next_delay(self) -> float:
        base = min(self.initial_s * (self.factor**self._attempt), self.max_s)
        spread = random.uniform(1.0 - self.jitter, 1.0 + self.jitter)
        self._attempt += 1
        return max(0.0, base * spread)

    def reset(self) -> None:
        self._attempt = 0

    @property
    def attempts(self) -> int:
        return self._attempt

    @property
    def exhausted(self) -> bool:
        return self.max_attempts is not None and self._attempt >= self.max_attempts
