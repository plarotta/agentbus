"""Layer 3 — Bus server (the WebSocket daemon).

A thin shell wrapping a ``LocalTarget`` (a ``BusCore`` or ``MessageBus.local_target()``)
in a WebSocket server. It owns no routing logic of its own — every accepted
``publish`` goes through the target's ``publish`` (registry validation → hooks →
log → fan-out), and every ``subscribe`` attaches a per-connection sink to the
target. The sink enqueues onto an outbound queue that a per-connection writer
task drains to the socket, keeping the synchronous ``Sink.deliver`` contract
while real I/O stays async.

Named ``BusServer`` (not "daemon") to avoid colliding with ``daemon.py``, which
is the process-supervisor for ``agentbus launch``.
"""

import asyncio
import json
import logging
from typing import Any

from agentbus.contracts import Envelope
from agentbus.core import SinkClosed
from agentbus.message import Message
from agentbus.transport import LocalTarget, _env_to_wire, _require_websockets

logger = logging.getLogger(__name__)


class _ConnectionSink:
    """Core sink for one WebSocket connection: serialize each delivered Message
    to an Envelope and enqueue it onto the connection's outbound queue. Raises
    ``SinkClosed`` once the connection is gone so the core prunes it.

    The queue is unbounded: a resume can replay an arbitrary backlog that must
    not be dropped (drop-oldest would silently skip messages right after the
    resume offset). A pathologically slow client therefore grows memory until it
    disconnects — an accepted v1 trade-off for lossless replay.
    """

    def __init__(self, queue: "asyncio.Queue[Envelope]") -> None:
        self._queue = queue
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def deliver(self, msg: Message) -> None:
        if self._closed:
            raise SinkClosed("connection closed")
        self._queue.put_nowait(Envelope.from_message(msg))


class BusServer:
    """WebSocket front door for a ``LocalTarget``.

    Usage::

        server = BusServer(bus.local_target())
        await server.start()           # binds an ephemeral port by default
        client = BusClient(WebSocketTransport(server.url), name="remote")
        ...
        await server.stop()
    """

    def __init__(self, target: LocalTarget, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._ws = _require_websockets()
        self._target = target
        self._host = host
        self.port = port
        self._server: Any = None

    @property
    def url(self) -> str:
        return f"ws://{self._host}:{self.port}"

    async def start(self) -> None:
        self._server = await self._ws.serve(self._handle, self._host, self.port)
        # Resolve the actual bound port when started with port=0.
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, conn: Any) -> None:
        outbound: asyncio.Queue[Envelope] = asyncio.Queue()
        sink = _ConnectionSink(outbound)
        unsubs: list = []
        writer = asyncio.create_task(self._writer(conn, outbound))
        try:
            async for raw in conn:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                op = data.get("op")
                if op == "publish":
                    self._on_publish(data)
                elif op == "subscribe":
                    pattern = data.get("pattern")
                    if isinstance(pattern, str):
                        unsubs.append(self._on_subscribe(pattern, data.get("from_offset"), sink))
        except self._ws.ConnectionClosed:
            pass
        finally:
            sink.close()
            for unsub in unsubs:
                unsub()
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    def _on_subscribe(self, pattern: str, from_offset: Any, sink: _ConnectionSink):
        """Attach the live sink and, if resuming, replay history ahead of it.

        Synchronous start-to-finish (no await), so attaching the sink and
        snapshotting the cutover offset happen atomically — no publish can
        interleave. Replayed messages (offset ≤ cutover) are enqueued before any
        live message (offset > cutover), so the client sees a gap-free stream.
        """
        if from_offset is None:
            return self._target.subscribe(pattern, sink)
        cutover = self._target.latest_offset()
        unsub = self._target.subscribe(pattern, sink)
        for msg in self._target.replay(from_offset=int(from_offset), topic=pattern):
            if msg.offset is not None and msg.offset > cutover:
                break
            sink.deliver(msg)
        return unsub

    def _on_publish(self, data: dict) -> None:
        try:
            env = Envelope.model_validate(data.get("env"))
            msg = env.to_message(registry=self._target.schemas)
        except Exception as e:
            # Bad input from a client must not take down the connection.
            logger.warning("BusServer dropping invalid publish: %s", e)
            return
        self._target.publish(msg)

    async def _writer(self, conn: Any, outbound: "asyncio.Queue[Envelope]") -> None:
        try:
            while True:
                env = await outbound.get()
                await conn.send(_env_to_wire("message", env))
        except (asyncio.CancelledError, self._ws.ConnectionClosed):
            return
