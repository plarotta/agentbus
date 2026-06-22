"""Layer 2 — Bus core.

The routing heart, extracted from ``MessageBus``: an append-only log, a
pattern-keyed subscription table, a hook chain, and synchronous fan-out to
abstract sinks. It knows nothing about asyncio node loops, sockets, ``Topic``
objects, or the harness — it moves ``Message`` envelopes from publishers to
sinks and records them in the log.

Why synchronous: ``MessageBus.publish`` is sync and called from many sync
contexts; making the core async would ripple ``await`` through every caller.
A ``Sink.deliver`` is therefore a *non-blocking enqueue* — a transport sink
drops the message into an outbound ``asyncio.Queue`` that a separate async task
drains to the wire. That keeps the core sync while real I/O stays async.

Hooks are likewise synchronous (``Message -> Message | None``): the policy /
redaction / audit chokepoint. Returning ``None`` blocks the message; returning a
(possibly modified) message passes it on. Async hooks are a deliberate later
extension.

The dependency arrow points one way: ``core`` → ``contracts``. Nothing here
imports the bus, the node runtime, or any transport.
"""

import contextlib
import logging
from collections import deque
from collections.abc import Callable, Iterator
from typing import Protocol, runtime_checkable

from agentbus.contracts import TOPIC_SCHEMAS, schema_for
from agentbus.errors import TopicSchemaError
from agentbus.message import Message
from agentbus.topic import _match_pattern

logger = logging.getLogger(__name__)

LOG_MAXLEN_DEFAULT = 10_000

# A hook inspects/transforms an envelope at publish time. Return the (possibly
# modified) message to pass it on, or None to drop it before it is logged or
# fanned out.
Hook = Callable[[Message], Message | None]


class SinkClosed(Exception):
    """Raised by a sink's ``deliver`` when its destination is permanently gone.

    The core prunes any sink that raises this — a dead websocket, a closed
    client. Transient backpressure is *not* a SinkClosed: a full queue drops a
    message (per policy) but the sink stays subscribed.
    """


@runtime_checkable
class Sink(Protocol):
    """A destination the core fans messages out to.

    ``deliver`` must not block — enqueue and return. Raise ``SinkClosed`` when
    the destination is permanently unreachable so the core can prune it.
    """

    def deliver(self, msg: Message) -> None: ...


class QueueSink:
    """Sink that enqueues onto a bounded ``asyncio.Queue`` with a drop policy.

    The general-purpose local sink: a consumer (node loop, client receive loop)
    awaits ``queue.get()``. On overflow it applies ``drop-oldest`` (evict the
    head to make room) or ``drop-newest`` (discard the incoming message), and
    invokes the optional ``on_drop`` callback with the dropped ``Message`` so the
    caller can surface a backpressure event. ``close()`` makes subsequent
    deliveries raise ``SinkClosed``.
    """

    def __init__(
        self,
        queue,
        *,
        policy: str = "drop-oldest",
        on_drop: Callable[[Message], None] | None = None,
    ) -> None:
        self.queue = queue
        self.policy = policy
        self._on_drop = on_drop
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def deliver(self, msg: Message) -> None:
        import asyncio

        if self._closed:
            raise SinkClosed("sink closed")
        try:
            self.queue.put_nowait(msg)
            return
        except asyncio.QueueFull:
            pass

        if self.policy == "drop-oldest":
            try:
                dropped = self.queue.get_nowait()
                self.queue.put_nowait(msg)
            except asyncio.QueueEmpty:
                self.queue.put_nowait(msg)
                return
            if self._on_drop is not None:
                self._on_drop(dropped)
        else:  # drop-newest
            if self._on_drop is not None:
                self._on_drop(msg)


class CallbackSink:
    """Sink that calls a plain function for each message. Handy for tests and
    in-process taps (e.g. an inspector). Never raises ``SinkClosed`` on its own.
    """

    def __init__(self, fn: Callable[[Message], None]) -> None:
        self._fn = fn

    def deliver(self, msg: Message) -> None:
        self._fn(msg)


@runtime_checkable
class LogStore(Protocol):
    """Append-only message log behind the bus — the source of truth for replay.

    Assigns a monotonic, never-reused ``offset`` to each appended message and
    reads them back by offset. ``tail`` is a bounded in-memory deque of the most
    recent messages, kept for fast introspection (``/trace``, history) regardless
    of whether the store is durable.
    """

    tail: "deque[Message]"

    def append(self, msg: Message) -> Message:
        """Record msg, stamp it with its assigned offset, return the stamped copy."""
        ...

    def read(
        self, *, from_offset: int = 0, topic: str | None = None, limit: int | None = None
    ) -> Iterator[Message]:
        """Yield stored messages with ``offset > from_offset``, in offset order.

        ``topic`` filters by wildcard pattern; ``limit`` caps the count.
        """
        ...

    def latest_offset(self) -> int:
        """Highest offset assigned so far (0 if none). Monotonic across restarts."""
        ...

    def prune(self, before_offset: int) -> int:
        """Drop messages with ``offset < before_offset``. Returns the count removed."""
        ...


class InMemoryLog:
    """Volatile ``LogStore`` backed by a bounded deque — the default store.

    Preserves the original in-memory log behavior: not durable across restarts,
    offsets reset on construction. ``read`` is best-effort — it can only return
    messages still in the (bounded) tail.
    """

    def __init__(self, maxlen: int = LOG_MAXLEN_DEFAULT) -> None:
        self.tail: deque[Message] = deque(maxlen=maxlen)
        self._counter = 0

    def append(self, msg: Message) -> Message:
        self._counter += 1
        stamped = msg.model_copy(update={"offset": self._counter})
        self.tail.append(stamped)
        return stamped

    def read(
        self, *, from_offset: int = 0, topic: str | None = None, limit: int | None = None
    ) -> Iterator[Message]:
        count = 0
        for m in list(self.tail):
            if m.offset is None or m.offset <= from_offset:
                continue
            if topic is not None and not _match_pattern(topic, m.topic):
                continue
            yield m
            count += 1
            if limit is not None and count >= limit:
                return

    def latest_offset(self) -> int:
        return self._counter

    def prune(self, before_offset: int) -> int:
        removed = 0
        while self.tail and self.tail[0].offset is not None and self.tail[0].offset < before_offset:
            self.tail.popleft()
            removed += 1
        return removed


class _Subscription:
    __slots__ = ("pattern", "sink")

    def __init__(self, pattern: str, sink: Sink) -> None:
        self.pattern = pattern
        self.sink = sink


class BusCore:
    """Append-only log + subscription table + hooks + fan-out.

    ``publish`` is the single write path: validate against the schema registry,
    run the hook chain (block/transform), append to the log (the log is truth —
    it records what was accepted, *before* fan-out), then deliver to every sink
    whose pattern matches the topic, pruning any that report themselves closed.
    """

    def __init__(
        self,
        *,
        schemas: dict | None = None,
        log_maxlen: int = LOG_MAXLEN_DEFAULT,
        log_store: LogStore | None = None,
    ) -> None:
        # Default to the module-global registry that Topic[T] populates; tests
        # and isolated cores can pass their own dict.
        self.schemas = TOPIC_SCHEMAS if schemas is None else schemas
        # The log assigns offsets and is the source of truth for replay. Default
        # is the volatile InMemoryLog; pass a SqliteLog for durable replay.
        self._store: LogStore = (
            log_store if log_store is not None else InMemoryLog(maxlen=log_maxlen)
        )
        self.hooks: list[Hook] = []
        self._subs: list[_Subscription] = []

    @property
    def log(self) -> "deque[Message]":
        """Bounded in-memory tail of recent messages (introspection / trace)."""
        return self._store.tail

    def latest_offset(self) -> int:
        """Highest offset assigned by the log so far (0 if none)."""
        return self._store.latest_offset()

    # ── Subscriptions ──────────────────────────────────────────────────────

    def subscribe(self, pattern: str, sink: Sink) -> Callable[[], None]:
        """Subscribe a sink to a topic pattern (``*`` / ``**`` supported).

        Returns an ``unsubscribe()`` callable. Matching is resolved at publish
        time, so a sink subscribed to ``**`` (the inspector pattern) sees every
        topic, including topics that did not exist when it subscribed.
        """
        sub = _Subscription(pattern, sink)
        self._subs.append(sub)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subs.remove(sub)

        return _unsubscribe

    def add_hook(self, hook: Hook) -> Callable[[], None]:
        """Append a hook to the chain. Returns a callable that removes it."""
        self.hooks.append(hook)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self.hooks.remove(hook)

        return _remove

    # ── Validation ─────────────────────────────────────────────────────────

    def validate(self, msg: Message) -> None:
        """Defense-in-depth schema check against the registry.

        Topics with no registered schema are passed through (the core does not
        own the topic namespace). Registered topics must carry a payload that is
        an instance of the bound model.
        """
        model = schema_for(msg.topic, registry=self.schemas)
        if model is None:
            return
        if not isinstance(msg.payload, model):
            raise TopicSchemaError(
                f"Topic {msg.topic!r} expects {model.__name__}, got {type(msg.payload).__name__}"
            )

    # ── Publish ────────────────────────────────────────────────────────────

    def publish(self, msg: Message) -> Message | None:
        """Validate → hooks → append log → fan out. Returns the delivered
        message, or ``None`` if a hook blocked it.
        """
        self.validate(msg)

        for hook in self.hooks:
            result = hook(msg)
            if result is None:
                return None
            msg = result

        # Append BEFORE fan-out: the log is the record of what was accepted. The
        # store assigns the offset and returns the stamped message — subscribers
        # see the offset so they can checkpoint and resume from it.
        msg = self._store.append(msg)

        dead: list[_Subscription] = []
        for sub in list(self._subs):
            if _match_pattern(sub.pattern, msg.topic):
                try:
                    sub.sink.deliver(msg)
                except SinkClosed:
                    dead.append(sub)
                except Exception as e:  # a sink must not break fan-out
                    logger.error("sink error on topic %r: %s", msg.topic, e)
        for sub in dead:
            with contextlib.suppress(ValueError):
                self._subs.remove(sub)

        return msg

    # ── Replay ─────────────────────────────────────────────────────────────

    def replay(
        self,
        *,
        from_offset: int = 0,
        topic: str | None = None,
        limit: int | None = None,
    ) -> Iterator[Message]:
        """Iterate logged messages with ``offset > from_offset`` for catch-up.

        ``topic`` filters by wildcard pattern; ``limit`` caps the count. Delegates
        to the configured ``LogStore`` — durable when backed by a ``SqliteLog``,
        best-effort over the recent tail when backed by the default ``InMemoryLog``.
        """
        return self._store.read(from_offset=from_offset, topic=topic, limit=limit)
