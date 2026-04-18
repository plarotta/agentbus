"""Stall watchdog — detect transport stalls that manifest as silent idle.

A gateway's listen loop can enter a zombie state where the transport
neither errors nor produces events. slack-bolt's Socket Mode has its
own heartbeat so it usually self-corrects; Telegram long-poll will
eventually time out. But both can get stuck behind a half-open TCP
connection, a stalled TLS handshake, or a misbehaving load balancer
that accepts bytes and replies to none of them.

The watchdog runs as a background task: the listener calls
``heartbeat()`` on every successful activity (poll round, event
dispatch). If ``idle_s`` passes without a heartbeat, the watchdog
fires its ``on_stall`` callback exactly once. Callers typically cancel
the listener task from ``on_stall`` so the gateway's reconnect loop
kicks in with a fresh transport.

This is intentionally lightweight — no thread, no timers, just an
``asyncio.sleep`` loop that polls ``time.monotonic()``. The resolution
(``check_interval_s``, default 1s) is the worst-case lag between a
stall beginning and the callback firing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

OnStall = Callable[[], Awaitable[None]]


class StallWatchdog:
    """Fires ``on_stall`` once if ``heartbeat()`` hasn't been called in ``idle_s``."""

    def __init__(
        self,
        *,
        idle_s: float,
        on_stall: OnStall,
        check_interval_s: float = 1.0,
    ) -> None:
        if idle_s <= 0:
            raise ValueError("idle_s must be positive")
        self._idle_s = idle_s
        self._on_stall = on_stall
        self._interval = check_interval_s
        self._last = time.monotonic()
        self._task: asyncio.Task[None] | None = None
        self._fired = False

    def heartbeat(self) -> None:
        """Record fresh activity. Resets the fired flag too, so a later
        stall can be detected after a recovery."""
        self._last = time.monotonic()
        self._fired = False

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._last = time.monotonic()
        self._fired = False
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                if self._fired:
                    continue
                if (time.monotonic() - self._last) >= self._idle_s:
                    self._fired = True
                    try:
                        await self._on_stall()
                    except Exception as exc:  # pragma: no cover
                        logger.warning("StallWatchdog on_stall raised: %s", exc)
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    @property
    def fired(self) -> bool:
        return self._fired
