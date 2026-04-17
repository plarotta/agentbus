import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

from agentbus.errors import (
    DuplicateNodeError,
    DuplicateTopicError,
    RequestTimeoutError,
    UndeclaredPublicationError,
    UndeclaredSubscriptionError,
)
from agentbus.introspection import (
    BusGraph,
    Edge,
    NodeInfo,
    NodeStats,
    SpinResult,
    TopicInfo,
)
from agentbus.message import Message
from agentbus.node import Node, NodeHandle, NodeState
from agentbus.schemas.system import BackpressureEvent, Heartbeat, LifecycleEvent, TelemetryEvent
from agentbus.topic import Topic, _match_pattern

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_NODE_ERRORS = 10
HEARTBEAT_INTERVAL_DEFAULT = 30.0
INIT_TIMEOUT_DEFAULT = 30.0
MESSAGE_LOG_MAXLEN = 10_000


class _BusHandle:
    """Concrete BusHandle implementation given to nodes in on_init().

    Satisfies the BusHandle Protocol in node.py structurally. Holds a
    back-reference to the bus and the owning node's name so that publish()
    can enforce declared-publication checks and stamp source_node correctly.
    """

    def __init__(self, bus: "MessageBus", node_name: str) -> None:
        self._bus = bus
        self._node_name = node_name

    async def publish(self, topic: str, payload: Any, correlation_id: str | None = None) -> None:
        handle = self._bus._nodes.get(self._node_name)
        if handle is None:
            raise RuntimeError(f"Node {self._node_name!r} not found in bus")
        node = handle.node
        if not any(_match_pattern(p, topic) for p in node.publications):
            raise UndeclaredPublicationError(
                f"Node {self._node_name!r} cannot publish to {topic!r}: "
                f"not in declared publications {node.publications!r}"
            )
        handle.messages_published += 1
        self._bus.publish(
            topic, payload, source_node=self._node_name, correlation_id=correlation_id
        )

    async def request(
        self,
        topic: str,
        payload: Any,
        reply_on: str,
        *,
        timeout: float = 30.0,
    ) -> Message:
        cid = str(uuid4())
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._bus._pending_requests[(reply_on, cid)] = future
        try:
            await self.publish(topic, payload, correlation_id=cid)
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            raise RequestTimeoutError(
                f"Request on {topic!r} timed out after {timeout}s waiting on {reply_on!r}"
            ) from None
        finally:
            self._bus._pending_requests.pop((reply_on, cid), None)
            if not future.done():
                future.cancel()

    async def topic_history(self, topic: str, n: int = 10) -> list[Message]:
        t = self._bus._topics.get(topic)
        if t is None:
            return []
        return t.history(n)


class MessageBus:
    """Typed pub/sub message bus for LLM agent orchestration.

    Usage::

        bus = MessageBus()
        bus.register_topic(Topic[InboundChat]("/inbound/chat"))
        bus.register_node(PlannerNode())
        await bus.spin(max_messages=10)
    """

    def __init__(
        self,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_DEFAULT,
        message_log_maxlen: int = MESSAGE_LOG_MAXLEN,
        socket_path: str | None = "/tmp/agentbus.sock",
    ) -> None:
        self._topics: dict[str, Topic] = {}
        self._nodes: dict[str, NodeHandle] = {}
        self._message_log: deque[Message] = deque(maxlen=message_log_maxlen)
        self._running = False
        self._total_messages = 0
        self._start_time: float = 0.0
        self._heartbeat_interval = heartbeat_interval
        self._pending_requests: dict[tuple[str, str], asyncio.Future] = {}
        self._publishing_backpressure = False
        self._socket_path = socket_path

        # Auto-register system topics
        self.register_topic(Topic[LifecycleEvent]("/system/lifecycle", retention=100))
        self.register_topic(Topic[Heartbeat]("/system/heartbeat", retention=1))
        self.register_topic(Topic[BackpressureEvent]("/system/backpressure"))
        self.register_topic(Topic[TelemetryEvent]("/system/telemetry", retention=50))

    # ── Registration ──────────────────────────────────────────────────────────

    def register_topic(self, topic: Topic) -> None:
        """Register a topic. Raises DuplicateTopicError on name collision."""
        if topic.name in self._topics:
            raise DuplicateTopicError(f"Topic {topic.name!r} is already registered")
        self._topics[topic.name] = topic

    def register_node(self, node: Node) -> None:
        """Register a node, wiring its subscriptions and validating its publications.

        Raises DuplicateNodeError, UndeclaredSubscriptionError, or
        UndeclaredPublicationError as appropriate.
        """
        if node.name in self._nodes:
            raise DuplicateNodeError(f"Node {node.name!r} is already registered")

        handle = NodeHandle(node, max_errors=MAX_CONSECUTIVE_NODE_ERRORS)

        # Wire subscriptions — each matching topic gets a reference to this node's queue
        for pattern in node.subscriptions:
            matching = [t for t in self._topics.values() if t.matches(pattern)]
            if not matching:
                raise UndeclaredSubscriptionError(
                    f"Node {node.name!r} subscription {pattern!r} matches no registered topics"
                )
            for t in matching:
                t.add_subscriber(node.name, handle.queue)

        # Validate publications — every pattern must resolve to at least one topic
        for pattern in node.publications:
            matching = [t for t in self._topics.values() if _match_pattern(pattern, t.name)]
            if not matching:
                raise UndeclaredPublicationError(
                    f"Node {node.name!r} publication {pattern!r} matches no registered topics"
                )

        self._nodes[node.name] = handle

    # ── Publishing ────────────────────────────────────────────────────────────

    def publish(
        self,
        topic_name: str,
        payload: Any,
        *,
        source_node: str = "_bus_",
        correlation_id: str | None = None,
    ) -> Message:
        """Build a Message envelope, fan it out to subscribers, and return it.

        This is the bus-internal publish used by _BusHandle and the bus itself
        (heartbeat, lifecycle events). Nodes publish via BusHandle.publish().
        """
        topic = self._topics[topic_name]
        topic.validate_payload(payload)

        msg = Message(
            source_node=source_node,
            topic=topic_name,
            correlation_id=correlation_id,
            payload=payload,
        )

        # Check if any pending request/reply future should be resolved
        if correlation_id and (topic_name, correlation_id) in self._pending_requests:
            future = self._pending_requests[(topic_name, correlation_id)]
            if not future.done():
                future.set_result(msg)

        events = topic.put(msg)
        self._message_log.append(msg)
        self._total_messages += 1

        # Publish backpressure events — guard against infinite recursion
        if events and not self._publishing_backpressure:
            self._publishing_backpressure = True
            try:
                for ev in events:
                    self.publish("/system/backpressure", ev, source_node="_bus_")
            finally:
                self._publishing_backpressure = False

        return msg

    # ── Message dispatch (shared by spin_once and _node_loop) ─────────────────

    async def _dispatch_message(self, handle: NodeHandle, msg: Message) -> None:
        async with handle.semaphore:
            try:
                await handle.node.on_message(msg)
                handle.error_breaker.record_success()
            except Exception as e:
                handle.errors += 1
                handle.error_breaker.record_failure()
                logger.error("Node %r on_message error: %s", handle.node.name, e)
                self.publish(
                    "/system/lifecycle",
                    LifecycleEvent(node=handle.node.name, event="error", error=str(e)),
                )
                if handle.error_breaker.is_open:
                    handle.state = NodeState.ERROR
            finally:
                handle.messages_received += 1

    # ── spin_once ─────────────────────────────────────────────────────────────

    async def spin_once(self, timeout: float = 5.0) -> Message | None:
        """Process exactly one queued message across all registered nodes.

        Primary testing primitive. Works regardless of whether spin() has been
        called — nodes do not need to be in RUNNING state.

        Returns the processed message, or None if no message arrives within timeout.
        """
        handles = [h for h in self._nodes.values() if h.state != NodeState.ERROR]
        if not handles:
            return None

        # Fast path: check for immediately available messages
        for h in handles:
            try:
                msg = h.queue.get_nowait()
                await self._dispatch_message(h, msg)
                return msg
            except asyncio.QueueEmpty:
                pass

        # Slow path: wait for the first message to arrive
        async def _get(h: NodeHandle) -> tuple[NodeHandle, Message]:
            return (h, await h.queue.get())

        tasks = [asyncio.create_task(_get(h)) for h in handles]
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if not done:
            return None

        handle, msg = (next(iter(done))).result()
        await self._dispatch_message(handle, msg)
        return msg

    # ── spin ──────────────────────────────────────────────────────────────────

    async def spin(
        self,
        until: Callable[[], bool] | None = None,
        max_messages: int | None = None,
        timeout: float | None = None,
    ) -> SpinResult:
        """Run the bus through its four lifecycle phases and return a SpinResult.

        Termination:
          - ``until``: stop when the callable returns True (checked after each message)
          - ``max_messages``: stop after this many messages are processed
          - ``timeout``: stop after this many seconds (wall clock)
          - No args: run until cancelled (e.g. KeyboardInterrupt / asyncio.CancelledError)
        """
        start_ts = time.monotonic()

        # ── VALIDATION ────────────────────────────────────────────────────────
        self._validate()

        # ── INIT ──────────────────────────────────────────────────────────────
        await self._init_phase()
        self._start_time = time.monotonic()

        # ── SPIN ──────────────────────────────────────────────────────────────
        self._running = True
        stop_event = asyncio.Event()
        processed: list[int] = [0]  # mutable int for asyncio tasks to share

        async def _node_loop(handle: NodeHandle) -> None:
            while not stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(handle.queue.get(), timeout=0.05)
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    return

                await self._dispatch_message(handle, msg)
                processed[0] += 1

                if handle.state == NodeState.ERROR:
                    return  # circuit breaker tripped — exit this node's loop

                if max_messages is not None and processed[0] >= max_messages:
                    stop_event.set()
                    return
                if until is not None and until():
                    stop_event.set()
                    return

        # Short-circuit: max_messages=0 means process nothing
        if max_messages is not None and max_messages == 0:
            stop_event.set()

        loop_tasks = [
            asyncio.create_task(_node_loop(h))
            for h in self._nodes.values()
            if h.state == NodeState.RUNNING
        ]

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        socket_task = (
            asyncio.create_task(self._socket_server(self._socket_path))
            if self._socket_path is not None
            else None
        )

        # Wait for a termination condition
        try:
            if timeout is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=timeout)
            else:
                await stop_event.wait()
        except TimeoutError:
            pass  # timeout elapsed — proceed to shutdown

        # ── SHUTDOWN ──────────────────────────────────────────────────────────
        self._running = False
        stop_event.set()

        for t in loop_tasks:
            t.cancel()
        await asyncio.gather(*loop_tasks, return_exceptions=True)

        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

        if socket_task is not None:
            socket_task.cancel()
            await asyncio.gather(socket_task, return_exceptions=True)

        await self._shutdown_phase()

        duration = time.monotonic() - start_ts
        per_node = {
            name: NodeStats(
                messages_received=h.messages_received,
                messages_published=h.messages_published,
                errors=h.errors,
            )
            for name, h in self._nodes.items()
        }
        return SpinResult(
            messages_processed=processed[0],
            duration_s=duration,
            per_node=per_node,
        )

    # ── Socket server ─────────────────────────────────────────────────────────

    async def _socket_server(self, path: str) -> None:
        """Serve introspection commands over a Unix domain socket.

        Protocol: newline-delimited JSON. Each request is a JSON object with a
        "cmd" key. Responses are single JSON lines, except for "echo" which
        streams one line per message until the client disconnects or the
        requested count is reached.

        Commands: topics, nodes, node_info, graph, history, echo
        """
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

        def _encode(data: object) -> bytes:
            return (json.dumps(data) + "\n").encode()

        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError:
                        writer.write(_encode({"error": "invalid JSON"}))
                        await writer.drain()
                        continue

                    cmd = req.get("cmd", "")
                    if cmd == "topics":
                        writer.write(_encode([dataclasses.asdict(t) for t in self.topics()]))
                    elif cmd == "nodes":
                        writer.write(_encode([dataclasses.asdict(n) for n in self.nodes()]))
                    elif cmd == "node_info":
                        name = req.get("name")
                        for n_info in self.nodes():
                            if n_info.name == name:
                                writer.write(_encode(dataclasses.asdict(n_info)))
                                break
                        else:
                            writer.write(_encode({"error": f"node {name!r} not found"}))
                    elif cmd == "graph":
                        writer.write(_encode(dataclasses.asdict(self.graph())))
                    elif cmd == "history":
                        t_name = req.get("topic", "")
                        n = req.get("n", 10)
                        msgs = self.history(t_name, n)
                        writer.write(_encode([json.loads(m.model_dump_json()) for m in msgs]))
                    elif cmd == "echo":
                        t_name = req.get("topic", "")
                        n = req.get("n")

                        async def _stream(_t: str = t_name, _n: int | None = n) -> None:
                            async for msg in self.echo(_t, n=_n):
                                try:
                                    writer.write(_encode(json.loads(msg.model_dump_json())))
                                    await writer.drain()
                                except Exception:
                                    return

                        stream_task = asyncio.create_task(_stream())
                        disconnect_task = asyncio.create_task(reader.read(1))
                        _done, pending = await asyncio.wait(
                            {stream_task, disconnect_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for pt in pending:
                            pt.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return  # close connection after streaming ends
                    else:
                        writer.write(_encode({"error": f"unknown command: {cmd!r}"}))
                    await writer.drain()
            except Exception as exc:
                logger.debug("Socket client error: %s", exc)
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_unix_server(handle_client, path=path)
        try:
            async with server:
                await server.serve_forever()
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

    # ── Internal phases ───────────────────────────────────────────────────────

    def _validate(self) -> None:
        """VALIDATION phase: log warnings for topology issues, never raise."""
        node_publishers: set[str] = set()
        node_subscribers: set[str] = set()

        for handle in self._nodes.values():
            node = handle.node
            for p in node.publications:
                for t in self._topics.values():
                    if _match_pattern(p, t.name):
                        node_publishers.add(t.name)
            for p in node.subscriptions:
                for t in self._topics.values():
                    if t.matches(p):
                        node_subscribers.add(t.name)

        for name, _topic in self._topics.items():
            if name.startswith("/system/"):
                continue  # bus-owned — skip
            has_subs = name in node_subscribers
            has_pubs = name in node_publishers
            if not has_subs and not has_pubs:
                logger.warning("Orphan topic %r: no subscribers and no publishers", name)
            elif not has_subs:
                logger.warning("Topic %r has no subscribers", name)
            elif not has_pubs:
                logger.warning("Topic %r has no publishers", name)

    async def _init_phase(self) -> None:
        """INIT phase: call on_init() on all nodes in parallel, handle failures."""

        async def _init_one(handle: NodeHandle) -> None:
            bus_handle = _BusHandle(self, handle.node.name)
            try:
                await asyncio.wait_for(
                    handle.node.on_init(bus_handle), timeout=INIT_TIMEOUT_DEFAULT
                )
                handle.state = NodeState.RUNNING
                self.publish(
                    "/system/lifecycle",
                    LifecycleEvent(node=handle.node.name, event="started"),
                )
            except TimeoutError:
                handle.state = NodeState.ERROR
                self.publish(
                    "/system/lifecycle",
                    LifecycleEvent(
                        node=handle.node.name,
                        event="init_failed",
                        error="on_init timed out",
                    ),
                )
            except Exception as e:
                handle.state = NodeState.ERROR
                self.publish(
                    "/system/lifecycle",
                    LifecycleEvent(
                        node=handle.node.name,
                        event="init_failed",
                        error=str(e),
                    ),
                )

        await asyncio.gather(
            *[_init_one(h) for h in self._nodes.values()],
            return_exceptions=True,
        )

    async def _shutdown_phase(self) -> None:
        """SHUTDOWN phase: call on_shutdown() on all nodes, publish lifecycle events."""

        async def _shutdown_one(handle: NodeHandle) -> None:
            try:
                await asyncio.wait_for(handle.node.on_shutdown(), timeout=10.0)
            except Exception as e:
                logger.error("Node %r shutdown error: %s", handle.node.name, e)
            finally:
                handle.state = NodeState.STOPPED
                self.publish(
                    "/system/lifecycle",
                    LifecycleEvent(node=handle.node.name, event="stopped"),
                )

        await asyncio.gather(
            *[
                _shutdown_one(h)
                for h in self._nodes.values()
                if h.state in (NodeState.RUNNING, NodeState.ERROR)
            ],
            return_exceptions=True,
        )

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            if not self._running:
                break  # type: ignore[unreachable]  # flipped concurrently during sleep
            uptime = time.monotonic() - self._start_time
            self.publish(
                "/system/heartbeat",
                Heartbeat(
                    uptime_s=uptime,
                    node_count=len(self._nodes),
                    topic_count=len(self._topics),
                    total_messages=self._total_messages,
                    messages_per_second=self._total_messages / max(uptime, 1.0),
                    node_states={n: h.state.value for n, h in self._nodes.items()},
                    queue_depths={n: h.queue.qsize() for n, h in self._nodes.items()},
                ),
            )

    # ── Introspection ─────────────────────────────────────────────────────────

    def topics(self) -> list[TopicInfo]:
        return [
            TopicInfo(
                name=t.name,
                schema_name=t.schema.__name__,
                retention=t.retention,
                subscriber_count=len(t._subscribers),
                message_count=len(t._buffer),
                queue_depths={n: q.qsize() for n, q in t._subscribers.items()},
            )
            for t in self._topics.values()
        ]

    def nodes(self) -> list[NodeInfo]:
        return [
            NodeInfo(
                name=h.node.name,
                state=h.state.value,
                concurrency=h.node.concurrency,
                concurrency_mode=h.node.concurrency_mode,
                subscriptions=list(h.node.subscriptions),
                publications=list(h.node.publications),
                messages_received=h.messages_received,
                messages_published=h.messages_published,
                errors=h.errors,
            )
            for h in self._nodes.values()
        ]

    def graph(self) -> BusGraph:
        node_infos = self.nodes()
        topic_infos = self.topics()
        edges: list[Edge] = []
        for h in self._nodes.values():
            for p in h.node.subscriptions:
                for t in self._topics.values():
                    if t.matches(p):
                        edges.append(Edge(node=h.node.name, topic=t.name, direction="sub"))
            for p in h.node.publications:
                for t in self._topics.values():
                    if _match_pattern(p, t.name):
                        edges.append(Edge(node=h.node.name, topic=t.name, direction="pub"))
        return BusGraph(nodes=node_infos, topics=topic_infos, edges=edges)

    def history(self, topic: str, n: int = 10) -> list[Message]:
        """Return the last N messages from a topic's retention buffer.

        Returns an empty list if the topic doesn't exist or has retention=0.
        """
        t = self._topics.get(topic)
        if t is None:
            return []
        return t.history(n)

    async def wait_for(
        self,
        topic: str,
        predicate: Callable[[Message], bool],
        timeout: float,
    ) -> Message:
        """Block until a message satisfying predicate arrives on topic.

        Raises RequestTimeoutError if no matching message arrives within timeout,
        or if the topic is not registered.
        """
        t = self._topics.get(topic)
        if t is None:
            raise RequestTimeoutError(f"Topic {topic!r} is not registered")

        temp_name = f"_wait_{uuid4().hex[:8]}"
        temp_queue: asyncio.Queue[Message] = asyncio.Queue()
        t.add_subscriber(temp_name, temp_queue)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RequestTimeoutError(f"wait_for on {topic!r} timed out after {timeout}s")
                try:
                    msg = await asyncio.wait_for(temp_queue.get(), timeout=remaining)
                except TimeoutError:
                    raise RequestTimeoutError(
                        f"wait_for on {topic!r} timed out after {timeout}s"
                    ) from None
                if predicate(msg):
                    return msg
        finally:
            t.remove_subscriber(temp_name)

    async def echo(
        self,
        topic: str,
        n: int | None = None,
        filter: Callable[[Message], bool] | None = None,
    ) -> AsyncIterator[Message]:
        """Tap a topic's live message stream, yielding messages as they arrive.

        Adds a temporary subscriber for the duration of iteration. Yields at
        most n messages (None = unlimited). Only yields messages that satisfy
        filter (None = no filter).

        The async generator is cancellable — the temporary subscriber is always
        removed on exit, including on cancellation.
        """
        t = self._topics.get(topic)
        if t is None:
            return

        temp_name = f"_echo_{uuid4().hex[:8]}"
        temp_queue: asyncio.Queue[Message] = asyncio.Queue()
        t.add_subscriber(temp_name, temp_queue)

        count = 0
        try:
            while n is None or count < n:
                try:
                    msg = await asyncio.wait_for(temp_queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                if filter is None or filter(msg):
                    yield msg
                    count += 1
        finally:
            t.remove_subscriber(temp_name)
