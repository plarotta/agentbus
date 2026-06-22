"""Layer 5 — BusClient.

What agents actually touch. Wraps a ``Transport`` and adds the ergonomics:
typed publish, per-pattern subscription queues, and request/reply by correlation
ID. The one piece of real async plumbing is the background receive loop — a
single task that drains ``transport.receive()`` forever and routes each envelope
to the right place: a pending request future (matched by ``correlation_id``) or
every subscription queue whose pattern matches the topic.

A client is *not* a node. It declares nothing up front; it can publish to any
topic the bus knows and subscribe to any pattern. Nodes are for in-process
runtimes; clients are for everything reaching the bus through a transport
(remote processes, the inspector, tests).
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from uuid import uuid4

from pydantic import BaseModel

from agentbus.contracts import Envelope
from agentbus.errors import RequestTimeoutError
from agentbus.topic import _match_pattern
from agentbus.transport import Transport

logger = logging.getLogger(__name__)


class BusClient:
    """Ergonomic client over a ``Transport``.

    Usage::

        client = BusClient(InMemoryTransport(bus.local_target()), name="agent-1")
        await client.connect()
        q = await client.subscribe("/payments/**")
        await client.publish("/payments/exception", SomePayload(...))
        env = await q.get()
        ...
        await client.close()
    """

    def __init__(self, transport: Transport, name: str) -> None:
        self.t = transport
        self.name = name
        self._queues: dict[str, asyncio.Queue[Envelope]] = {}
        self._pending: dict[str, asyncio.Future[Envelope]] = {}
        self._recv_task: asyncio.Task | None = None
        self._connected = False

    async def connect(self) -> None:
        """Connect the transport and start the background receive loop."""
        if self._connected:
            return
        await self.t.connect()
        self._recv_task = asyncio.create_task(self._receive_loop())
        self._connected = True

    async def close(self) -> None:
        """Stop the receive loop and close the transport."""
        if not self._connected:
            return
        self._connected = False
        await self.t.close()
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
            self._recv_task = None
        # Fail any outstanding requests so awaiters don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    # ── Publish / subscribe ─────────────────────────────────────────────────

    async def publish(self, topic: str, payload: BaseModel) -> None:
        """Publish a typed payload. Validated at the call site (a malformed
        model never leaves this process) and re-validated by the bus on receipt.
        """
        await self._send(topic, payload)

    async def subscribe(
        self, pattern: str, from_offset: int | None = None
    ) -> "asyncio.Queue[Envelope]":
        """Subscribe to a topic pattern, returning the queue fed with matching
        envelopes. Idempotent: repeated calls for the same pattern return the
        same queue and announce the subscription only once (``from_offset`` is
        honored only on the first call for a pattern).

        Pass ``from_offset`` to resume: the queue first yields replayed envelopes
        with ``offset > from_offset``, then live ones, gap-free. Checkpoint by
        persisting ``env.offset`` of the last envelope you fully processed.
        """
        existing = self._queues.get(pattern)
        if existing is not None:
            return existing
        queue: asyncio.Queue[Envelope] = asyncio.Queue()
        self._queues[pattern] = queue
        await self.t.announce_subscription(pattern, from_offset)
        return queue

    async def stream(self, pattern: str) -> AsyncIterator[Envelope]:
        """Async-iterate envelopes for a pattern (convenience over subscribe)."""
        queue = await self.subscribe(pattern)
        while True:
            yield await queue.get()

    # ── Request / reply ─────────────────────────────────────────────────────

    async def request(
        self,
        topic: str,
        payload: BaseModel,
        reply_on: str,
        *,
        timeout: float = 30.0,
    ) -> Envelope:
        """Publish a request and await a correlated reply on ``reply_on``.

        A responder handles the request and publishes its reply to ``reply_on``
        carrying the same ``correlation_id``. The receive loop matches it to the
        pending future. Mirrors the in-process ``BusHandle.request`` contract so
        the same responder code serves both.
        """
        await self.subscribe(reply_on)  # ensure replies reach the receive loop
        cid = str(uuid4())
        future: asyncio.Future[Envelope] = asyncio.get_running_loop().create_future()
        self._pending[cid] = future
        try:
            await self._send(topic, payload, correlation_id=cid, reply_to=reply_on)
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            raise RequestTimeoutError(
                f"Request on {topic!r} timed out after {timeout}s waiting on {reply_on!r}"
            ) from None
        finally:
            self._pending.pop(cid, None)
            if not future.done():
                future.cancel()

    # ── Internals ────────────────────────────────────────────────────────────

    async def _send(
        self,
        topic: str,
        payload: BaseModel,
        *,
        correlation_id: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        env = Envelope(
            source_node=self.name,
            topic=topic,
            correlation_id=correlation_id,
            reply_to=reply_to,
            payload=payload.model_dump(),
        )
        await self.t.send(env)

    async def _receive_loop(self) -> None:
        try:
            async for env in self.t.receive():
                cid = env.correlation_id
                if cid is not None:
                    fut = self._pending.get(cid)
                    if fut is not None:
                        if not fut.done():
                            fut.set_result(env)
                        continue  # reply consumed by the request future
                for pattern, queue in self._queues.items():
                    if _match_pattern(pattern, env.topic):
                        queue.put_nowait(env)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("BusClient %r receive loop error: %s", self.name, e)
