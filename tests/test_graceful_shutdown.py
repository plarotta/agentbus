"""Tests for graceful shutdown: drain_timeout, signal handlers, atomic session writes."""

import asyncio
import json
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from agentbus.bus import MessageBus
from agentbus.harness.session import Session, _atomic_write_text
from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.common import InboundChat
from agentbus.schemas.harness import ConversationTurn
from agentbus.topic import Topic


def make_bus() -> MessageBus:
    return MessageBus(socket_path=None)


def inbound(text: str = "hi") -> InboundChat:
    return InboundChat(channel="cli", sender="user", text=text)


def register_inbound(bus: MessageBus) -> Topic:
    t = Topic[InboundChat]("/inbound")
    bus.register_topic(t)
    return t


# ── drain_timeout ─────────────────────────────────────────────────────────────


async def test_drain_timeout_lets_queued_messages_finish():
    """With drain_timeout > 0, node loops should finish queued work after timeout fires."""
    bus = make_bus()
    register_inbound(bus)
    processed: list[str] = []

    class Slow(Node):
        name = "slow"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            await asyncio.sleep(0.05)
            processed.append(msg.payload.text)

    bus.register_node(Slow())

    # Queue 5 messages before spin starts.
    for i in range(5):
        bus.publish("/inbound", inbound(f"m{i}"))

    # timeout=0.01s forces shutdown almost immediately, with messages queued.
    # drain_timeout=3s should let the node loop keep pulling until empty.
    await bus.spin(timeout=0.01, drain_timeout=3.0)

    assert len(processed) == 5, f"expected all 5 drained, got {processed}"


async def test_drain_timeout_elapsed_forces_cancel():
    """If drain exceeds the timeout, node loops are force-cancelled."""
    bus = make_bus()
    register_inbound(bus)
    processed: list[str] = []

    class VerySlow(Node):
        name = "slow"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            await asyncio.sleep(5.0)  # far longer than drain window
            processed.append(msg.payload.text)

    bus.register_node(VerySlow())

    for i in range(3):
        bus.publish("/inbound", inbound(f"m{i}"))

    # drain_timeout=0.1s should elapse while message 0 is still sleeping.
    await bus.spin(max_messages=0, timeout=1.0, drain_timeout=0.1)
    # Nothing should have completed before force-cancel.
    assert processed == []


async def test_drain_timeout_zero_cancels_immediately():
    """Default drain_timeout=0 preserves pre-existing behaviour (immediate cancel)."""
    bus = make_bus()
    register_inbound(bus)
    processed: list[str] = []

    class Slow(Node):
        name = "slow"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            await asyncio.sleep(0.2)
            processed.append(msg.payload.text)

    bus.register_node(Slow())
    bus.publish("/inbound", inbound("m0"))
    bus.publish("/inbound", inbound("m1"))

    # No drain: only the in-flight message gets cancelled, nothing persisted.
    await bus.spin(max_messages=1, timeout=1.0)
    # Exactly one message was dispatched (max_messages=1 stops after 1).
    # With no drain, the remaining queued msg is dropped.
    assert len(processed) == 1


# ── signal handlers ──────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
async def test_signal_handler_triggers_graceful_shutdown():
    """SIGTERM in the running loop should trigger cooperative exit."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("loop.add_signal_handler requires main thread")

    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            pass

    bus.register_node(N())

    async def _send_sigterm_soon():
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = asyncio.create_task(_send_sigterm_soon())
    result = await bus.spin(install_signal_handlers=True, timeout=5.0)
    await sender

    # Should exit within a reasonable time (well under the 5s timeout).
    assert result.duration_s < 2.0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
async def test_second_signal_escalates_to_cancel():
    """A second signal mid-drain cancels node loops immediately."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("loop.add_signal_handler requires main thread")

    bus = make_bus()
    register_inbound(bus)

    class Stuck(Node):
        name = "stuck"
        subscriptions = ["/inbound"]
        publications = []

        async def on_message(self, msg: Message):
            await asyncio.sleep(10.0)

    bus.register_node(Stuck())
    bus.publish("/inbound", inbound())

    async def _send_two_signals():
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = asyncio.create_task(_send_two_signals())
    # Long drain_timeout — second signal should force cancel well before it.
    result = await bus.spin(
        install_signal_handlers=True,
        timeout=10.0,
        drain_timeout=10.0,
    )
    await sender
    assert result.duration_s < 2.0


def test_install_signal_handlers_noop_in_worker_thread():
    """Installing handlers off the main thread should be a silent no-op."""
    bus = make_bus()
    register_inbound(bus)

    class N(Node):
        name = "n"
        subscriptions = ["/inbound"]
        publications = []

    bus.register_node(N())

    result: list = []

    def _run():
        async def _inner():
            # Worker thread — add_signal_handler raises ValueError; the bus should
            # catch it, continue, and still complete.
            out = await bus.spin(install_signal_handlers=True, max_messages=0, timeout=1.0)
            result.append(out)

        asyncio.run(_inner())

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert len(result) == 1


# ── atomic session writes ───────────────────────────────────────────────────


def test_atomic_write_leaves_no_partial_file_on_crash(tmp_path: Path):
    """If the writer raises mid-write, neither the target nor temp should be left."""
    target = tmp_path / "session.json"

    # Seed an existing file — a crashed write must leave it untouched.
    target.write_text('{"old": true}', encoding="utf-8")

    class ExplodingStr(str):
        # os.fdopen(fd, "w") calls .write(text); make that raise.
        pass

    original = _atomic_write_text

    def _break_mid_write():
        fd, tmp_name = tempfile.mkstemp(dir=str(tmp_path), prefix=".session.json.", suffix=".tmp")
        tmp_path_obj = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("partial-content")
                raise RuntimeError("simulated crash mid-write")
        except RuntimeError:
            # Mirror _atomic_write_text's cleanup contract.
            tmp_path_obj.unlink(missing_ok=True)
            raise

    with pytest.raises(RuntimeError):
        _break_mid_write()

    # Target is still the old content, nothing half-written in place.
    assert target.read_text(encoding="utf-8") == '{"old": true}'
    # No orphaned temp files left behind.
    leftover = list(tmp_path.glob(".session.json.*.tmp"))
    assert leftover == [], f"leaked temp files: {leftover}"
    # Silence unused-name warnings.
    assert original is _atomic_write_text


def test_atomic_write_replaces_existing_file(tmp_path: Path):
    target = tmp_path / "out.json"
    target.write_text("old", encoding="utf-8")
    _atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    # No temp files left behind.
    assert list(tmp_path.glob(".out.json.*.tmp")) == []


def test_atomic_write_cleans_temp_on_exception(tmp_path: Path, monkeypatch):
    """If os.replace fails after the temp is written, temp must be removed."""
    target = tmp_path / "out.json"

    original_replace = os.replace

    def _boom(src, dst):
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        _atomic_write_text(target, "payload")
    monkeypatch.setattr(os, "replace", original_replace)

    # Target was never created.
    assert not target.exists()
    # Temp was cleaned up.
    assert list(tmp_path.glob(".out.json.*.tmp")) == []


def test_session_save_roundtrip_atomic(tmp_path: Path):
    turn = ConversationTurn(role="user", content="hello", token_count=3)
    session = Session("sess-1", turns=[turn], root_dir=tmp_path)
    session.save()

    # File exists and is valid JSON.
    data = json.loads((tmp_path / "sess-1" / "main.json").read_text())
    assert data["session_id"] == "sess-1"
    assert len(data["turns"]) == 1

    # No stray temp files.
    assert list((tmp_path / "sess-1").glob(".main.json.*.tmp")) == []

    loaded = Session.load("sess-1", root_dir=tmp_path)
    assert loaded.turns[0].content == "hello"
