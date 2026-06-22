"""Layer 4 — Transport.

The client-side seam between a ``BusClient`` and the bus. A transport moves
``Envelope``s (the dict-payload wire form) in both directions and nothing else —
it has no opinion about topics, schemas, or request/reply. That logic lives one
layer up in ``BusClient``.

Two implementations share one protocol so the *same* ``BusClient`` and the
*same* ``BusCore`` validation run in tests and over the wire:

- ``InMemoryTransport`` — calls a local target (a ``BusCore`` or a
  ``MessageBus.local_target()``) directly, in-process. No sockets, no
  serialization round-trip beyond Envelope↔Message. This is what tests and
  embedded single-process deployments use.
- ``WebSocketTransport`` (later) — serializes envelopes to a daemon.

The 5-method protocol: ``connect``, ``close``, ``send``, ``announce_subscription``,
``receive``.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, Protocol, runtime_checkable

from agentbus.contracts import Envelope
from agentbus.core import Sink
from agentbus.message import Message


@runtime_checkable
class Transport(Protocol):
    """Bidirectional envelope pipe between a client and the bus."""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def send(self, env: Envelope) -> None:
        """Publish an envelope to the bus."""
        ...

    async def announce_subscription(self, pattern: str, from_offset: int | None = None) -> None:
        """Tell the bus this client wants envelopes matching ``pattern``.

        If ``from_offset`` is given, the bus first replays logged messages with
        ``offset > from_offset`` (catch-up), then streams live — gap-free.
        """
        ...

    def receive(self) -> AsyncIterator[Envelope]:
        """Async-iterate envelopes delivered to this client until closed."""
        ...


@runtime_checkable
class LocalTarget(Protocol):
    """A local in-process bus an ``InMemoryTransport`` can drive directly.

    Both ``BusCore`` and ``MessageBus.local_target()`` satisfy this. The
    distinction matters: a bare ``BusCore`` reaches other transport sinks and the
    log/hooks; a ``MessageBus`` target *also* reaches in-process Topic nodes,
    because its ``publish`` runs the full bus pipeline.
    """

    schemas: dict

    def publish(self, msg: Message) -> Message | None: ...
    def subscribe(self, pattern: str, sink: Sink) -> Callable[[], None]: ...
    def latest_offset(self) -> int: ...
    def replay(
        self, *, from_offset: int = 0, topic: str | None = None, limit: int | None = None
    ) -> Iterator[Message]: ...


class _EnvelopeSink:
    """Core sink that serializes each delivered Message to an Envelope and
    enqueues it onto a transport's inbound queue (drop-oldest on overflow).
    """

    def __init__(self, queue: "asyncio.Queue[Envelope]") -> None:
        self._queue = queue

    def deliver(self, msg: Message) -> None:
        env = Envelope.from_message(msg)
        try:
            self._queue.put_nowait(env)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(env)
            except asyncio.QueueEmpty:
                self._queue.put_nowait(env)


# Sentinel pushed onto the inbound queue by close() to unblock a parked receive().
_CLOSE = object()


class InMemoryTransport:
    """Transport that drives a local ``LocalTarget`` (BusCore / MessageBus).

    ``send`` hydrates the envelope into a typed ``Message`` (re-validating
    against the target's schema registry — the same defense a daemon applies to
    wire traffic) and publishes it. ``announce_subscription`` attaches an
    ``_EnvelopeSink`` to the target so matching envelopes land on this
    transport's inbound queue, which ``receive`` drains.
    """

    def __init__(self, target: LocalTarget, *, inbound_maxsize: int = 1000) -> None:
        self._target = target
        self._inbound: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=inbound_maxsize)
        self._unsubs: list[Callable[[], None]] = []
        self._connected = False
        self._closed = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        # Unblock a parked receive().
        with contextlib.suppress(asyncio.QueueFull):
            self._inbound.put_nowait(_CLOSE)  # type: ignore[arg-type]

    async def send(self, env: Envelope) -> None:
        if not self._connected:
            raise RuntimeError("transport not connected")
        if self._closed:
            raise RuntimeError("transport closed")
        msg = env.to_message(registry=self._target.schemas)
        self._target.publish(msg)

    async def announce_subscription(self, pattern: str, from_offset: int | None = None) -> None:
        if self._closed:
            raise RuntimeError("transport closed")
        sink = _EnvelopeSink(self._inbound)
        if from_offset is None:
            self._unsubs.append(self._target.subscribe(pattern, sink))
            return
        # Resume: snapshot the cutover offset and attach the live sink in one
        # synchronous step (no await between → no publish can interleave), then
        # replay (from_offset .. cutover] ahead of the live stream. Best-effort
        # over an InMemoryLog (tail-bounded); durable with a SqliteLog target.
        cutover = self._target.latest_offset()
        self._unsubs.append(self._target.subscribe(pattern, sink))
        for msg in self._target.replay(from_offset=from_offset, topic=pattern):
            if msg.offset is not None and msg.offset > cutover:
                break
            sink.deliver(msg)

    async def receive(self) -> AsyncIterator[Envelope]:
        while not self._closed:
            env = await self._inbound.get()
            if env is _CLOSE:
                break
            yield env


def _require_websockets() -> Any:
    """Import the optional ``websockets`` package or raise with an install hint.

    Called at construction so a misconfigured WebSocket transport fails
    immediately, not mid-conversation.
    """
    try:
        import websockets
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "WebSocket transport requires the 'websockets' package. "
            "Install it with: uv sync --extra ws"
        ) from e
    return websockets


def _env_to_wire(op: str, env: Envelope) -> str:
    # model_dump_json applies Pydantic's encoder recursively (incl. datetimes
    # nested in the dict payload), so the result is always valid JSON.
    return json.dumps({"op": op, "env": json.loads(env.model_dump_json())})


class WebSocketTransport:
    """Transport that talks to a ``BusServer`` over a WebSocket connection.

    The wire protocol is JSON text frames, one op per frame:
    ``{"op": "publish", "env": {...}}`` and ``{"op": "subscribe", "pattern": ...}``
    client→server; ``{"op": "message", "env": {...}}`` server→client. Same
    ``BusClient`` and same server-side ``BusCore`` validation run here as over
    ``InMemoryTransport`` — only the bytes move differently.
    """

    def __init__(self, url: str) -> None:
        self._ws = _require_websockets()
        self._url = url
        self._conn: Any = None

    async def connect(self) -> None:
        self._conn = await self._ws.connect(self._url)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def send(self, env: Envelope) -> None:
        if self._conn is None:
            raise RuntimeError("transport not connected")
        await self._conn.send(_env_to_wire("publish", env))

    async def announce_subscription(self, pattern: str, from_offset: int | None = None) -> None:
        if self._conn is None:
            raise RuntimeError("transport not connected")
        op: dict[str, Any] = {"op": "subscribe", "pattern": pattern}
        if from_offset is not None:
            op["from_offset"] = from_offset
        await self._conn.send(json.dumps(op))

    async def receive(self) -> AsyncIterator[Envelope]:
        if self._conn is None:
            raise RuntimeError("transport not connected")
        try:
            async for raw in self._conn:
                data = json.loads(raw)
                if data.get("op") == "message":
                    yield Envelope.model_validate(data["env"])
        except self._ws.ConnectionClosed:
            return
