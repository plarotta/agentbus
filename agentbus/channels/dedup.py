"""Bounded inbound-event deduplication.

Slack redelivers events when a Socket Mode ack is lost (network
flap, process restart). Telegram's ``update_id`` + offset protocol
is already idempotent in theory, but a race between "ack the offset"
and "dispatch the update" can replay an update on reconnect. Without
a dedup cache the planner sees the same user message twice, tool
dispatches double up, and memory stores dupes.

:class:`DedupCache` is an LRU of recently seen event keys. The gateway
computes a natural key per channel (Slack: ``ts``; Telegram:
``update_id``) and asks whether it has seen it already. The cache is
bounded so it never grows unboundedly under a flood.
"""

from __future__ import annotations

from collections import OrderedDict


class DedupCache:
    """Fixed-capacity LRU of strings."""

    def __init__(self, capacity: int = 512) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._items: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity

    def check_and_add(self, key: str) -> bool:
        """Return ``True`` if ``key`` was already present (duplicate).

        Side-effect: the key's LRU position is refreshed on hit, and
        new keys are inserted. The oldest entry is evicted once the
        cache exceeds ``capacity``.
        """
        if key in self._items:
            self._items.move_to_end(key)
            return True
        self._items[key] = None
        if len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return False

    def add(self, key: str) -> None:
        """Insert ``key`` unconditionally (no duplicate signal)."""
        self.check_and_add(key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._items

    def __len__(self) -> int:
        return len(self._items)
