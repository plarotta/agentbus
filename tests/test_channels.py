"""Tests for agentbus.channels — contract, loader, Slack + Telegram gateways.

Network is mocked in every test. The Slack tests patch
``AsyncSocketModeHandler`` so no WebSocket is actually opened; the
Telegram tests inject a fake httpx client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, Field

from agentbus.channels import (
    ChannelPlugin,
    ChannelRuntimeError,
    load_channels_from_dict,
    open_channels_runtime,
    register_plugin,
    registered_plugins,
)
from agentbus.channels.base import MAX_CONSECUTIVE_GATEWAY_FAILURES
from agentbus.channels.chunking import (
    SLACK_TEXT_LIMIT,
    TELEGRAM_TEXT_LIMIT,
    chunk_text,
)
from agentbus.channels.dedup import DedupCache
from agentbus.channels.reconnect import ReconnectPolicy
from agentbus.channels.slack.config import SlackConfig
from agentbus.channels.telegram.config import TelegramConfig
from agentbus.channels.telegram.gateway import TelegramGatewayNode
from agentbus.channels.watchdog import StallWatchdog
from agentbus.gateway import GatewayNode
from agentbus.message import Message
from agentbus.schemas.common import InboundChat, OutboundChat
from agentbus.schemas.system import ChannelStatus

# ── Fake plugin fixture ──────────────────────────────────────────────────────


class _FakeConfig(BaseModel):
    token: str = Field(..., min_length=1)


class _FakeGateway(GatewayNode):
    name = "fake-gateway"
    channel_name = "fake"

    def __init__(self, config: _FakeConfig) -> None:
        super().__init__()
        self.config = config
        self.sent: list[Any] = []

    async def _listen_external(self) -> None:
        return None

    async def _send_external(self, msg: Message) -> None:
        self.sent.append(msg.payload)


class _FakePlugin(ChannelPlugin):
    name = "fake"
    ConfigModel = _FakeConfig

    @classmethod
    def create_gateway(cls, config: _FakeConfig) -> GatewayNode:
        return _FakeGateway(config)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Each test starts with the builtin plugins registered and nothing else."""
    from agentbus.channels.loader import _REGISTRY

    original = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(original)


# ── ChannelPlugin contract ───────────────────────────────────────────────────


class TestChannelPluginContract:
    def test_register_is_idempotent(self):
        register_plugin(_FakePlugin)
        register_plugin(_FakePlugin)
        assert registered_plugins()["fake"] is _FakePlugin

    def test_register_conflict_raises(self):
        class Other(ChannelPlugin):
            name = "fake"
            ConfigModel = _FakeConfig

            @classmethod
            def create_gateway(cls, config):
                raise NotImplementedError

        register_plugin(_FakePlugin)
        with pytest.raises(ChannelRuntimeError, match="conflict"):
            register_plugin(Other)

    def test_setup_wizard_default_raises(self):
        with pytest.raises(NotImplementedError):
            _FakePlugin.setup_wizard()


# ── load_channels_from_dict ──────────────────────────────────────────────────


class TestLoadChannelsFromDict:
    def test_empty_returns_empty(self):
        assert load_channels_from_dict(None) == []
        assert load_channels_from_dict({}) == []

    def test_rejects_non_mapping(self):
        with pytest.raises(ChannelRuntimeError, match="must be a mapping"):
            load_channels_from_dict(["slack"])

    def test_skips_disabled(self):
        register_plugin(_FakePlugin)
        out = load_channels_from_dict({"fake": False})
        assert out == []

    def test_skips_enabled_false(self):
        register_plugin(_FakePlugin)
        out = load_channels_from_dict({"fake": {"enabled": False, "token": "abc"}})
        assert out == []

    def test_validates_config(self):
        register_plugin(_FakePlugin)
        out = load_channels_from_dict({"fake": {"token": "abc"}})
        assert len(out) == 1
        plugin_cls, cfg = out[0]
        assert plugin_cls is _FakePlugin
        assert isinstance(cfg, _FakeConfig)
        assert cfg.token == "abc"

    def test_validation_error_surfaces(self):
        register_plugin(_FakePlugin)
        with pytest.raises(ChannelRuntimeError, match="Invalid config for channel"):
            load_channels_from_dict({"fake": {}})

    def test_unknown_plugin_raises(self):
        with pytest.raises(ChannelRuntimeError, match="Unknown channel plugin"):
            load_channels_from_dict({"not-a-real-thing": {}})

    def test_strips_enabled_before_validation(self):
        register_plugin(_FakePlugin)
        out = load_channels_from_dict({"fake": {"enabled": True, "token": "abc"}})
        assert out[0][1].token == "abc"


# ── open_channels_runtime ────────────────────────────────────────────────────


class TestOpenChannelsRuntime:
    async def test_builds_one_node_per_plugin(self):
        register_plugin(_FakePlugin)
        cfgs = load_channels_from_dict({"fake": {"token": "abc"}})
        runtime = await open_channels_runtime(cfgs)
        assert len(runtime.nodes) == 1
        assert isinstance(runtime.nodes[0], _FakeGateway)
        await runtime.aclose()


# ── GatewayNode channel filtering ────────────────────────────────────────────


class TestGatewayChannelFilter:
    async def test_drops_mismatched_channel(self):
        gateway = _FakeGateway(_FakeConfig(token="abc"))
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(text="hello", channel="other"),
            source_node="planner",
        )
        await gateway.on_message(msg)
        assert gateway.sent == []

    async def test_accepts_matching_channel(self):
        gateway = _FakeGateway(_FakeConfig(token="abc"))
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(text="hello", channel="fake"),
            source_node="planner",
        )
        await gateway.on_message(msg)
        assert len(gateway.sent) == 1

    async def test_accepts_none_channel(self):
        """Legacy single-channel mode: no channel set, everyone accepts."""
        gateway = _FakeGateway(_FakeConfig(token="abc"))
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(text="hello"),
            source_node="planner",
        )
        await gateway.on_message(msg)
        assert len(gateway.sent) == 1


# ── Slack config ─────────────────────────────────────────────────────────────


class TestSlackConfig:
    def test_requires_both_tokens(self):
        with pytest.raises(Exception):
            SlackConfig(bot_token="xoxb-1")
        with pytest.raises(Exception):
            SlackConfig(app_token="xapp-1")

    def test_empty_tokens_rejected(self):
        with pytest.raises(Exception):
            SlackConfig(app_token="", bot_token="xoxb-1")

    def test_allowlists_default_empty(self):
        cfg = SlackConfig(app_token="xapp-1", bot_token="xoxb-1")
        assert cfg.allowed_channels == []
        assert cfg.allowed_senders == []
        assert cfg.ignore_bots is True


# ── Slack gateway (inbound dispatch with mocked SDK) ─────────────────────────


@pytest.fixture
def slack_gateway(monkeypatch):
    """Return a SlackGatewayNode with slack-bolt classes mocked out."""
    from agentbus.channels.slack import gateway as slack_mod

    fake_app = MagicMock()
    fake_app.event.return_value = lambda fn: fn  # decorator no-op
    fake_app.client = MagicMock()
    fake_app.client.chat_postMessage = AsyncMock()
    fake_handler = MagicMock()
    fake_handler.start_async = AsyncMock()
    fake_handler.close_async = AsyncMock()

    monkeypatch.setattr(
        slack_mod,
        "_require_slack_sdk",
        lambda: (lambda token: fake_app, lambda app, tok: fake_handler),
    )

    cfg = SlackConfig(
        app_token="xapp-1",
        bot_token="xoxb-1",
        allowed_channels=["C-ALLOWED"],
        allowed_senders=["U-ALLOWED"],
    )
    node = slack_mod.SlackGatewayNode(cfg)
    node._app = fake_app
    node._handler = fake_handler
    # Inject a bus stub so publish_external/publish_channel_status don't need on_init.
    bus = MagicMock()
    bus.publish = AsyncMock()
    node._bus = bus
    return node, fake_app, bus


class TestSlackInbound:
    async def test_allowed_event_publishes_inbound(self, slack_gateway):
        node, _app, bus = slack_gateway
        await node._handle_event(
            {
                "user": "U-ALLOWED",
                "channel": "C-ALLOWED",
                "text": "hi",
                "ts": "1.0",
            }
        )
        bus.publish.assert_awaited()
        topic, payload = bus.publish.await_args.args
        assert topic == "/inbound"
        assert isinstance(payload, InboundChat)
        assert payload.channel == "slack"
        assert payload.metadata["slack_channel"] == "C-ALLOWED"
        assert payload.metadata["thread_ts"] == "1.0"

    async def test_bot_message_ignored(self, slack_gateway):
        node, _app, bus = slack_gateway
        await node._handle_event(
            {"user": "U", "channel": "C-ALLOWED", "text": "hi", "bot_id": "B1"}
        )
        bus.publish.assert_not_awaited()

    async def test_subtype_ignored(self, slack_gateway):
        node, _app, bus = slack_gateway
        await node._handle_event(
            {
                "user": "U",
                "channel": "C-ALLOWED",
                "text": "joined",
                "subtype": "channel_join",
            }
        )
        bus.publish.assert_not_awaited()

    async def test_channel_allowlist_filter(self, slack_gateway):
        node, _app, bus = slack_gateway
        await node._handle_event(
            {"user": "U-ALLOWED", "channel": "C-OTHER", "text": "hi", "ts": "1"}
        )
        bus.publish.assert_not_awaited()

    async def test_sender_allowlist_filter(self, slack_gateway):
        node, _app, bus = slack_gateway
        await node._handle_event(
            {"user": "U-OTHER", "channel": "C-ALLOWED", "text": "hi", "ts": "1"}
        )
        bus.publish.assert_not_awaited()

    async def test_empty_text_ignored(self, slack_gateway):
        node, _app, bus = slack_gateway
        await node._handle_event(
            {"user": "U-ALLOWED", "channel": "C-ALLOWED", "text": "", "ts": "1"}
        )
        bus.publish.assert_not_awaited()


class TestSlackOutbound:
    async def test_send_uses_metadata_channel_and_thread(self, slack_gateway):
        node, app, _bus = slack_gateway
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(
                text="reply",
                channel="slack",
                metadata={"slack_channel": "C-ALLOWED", "thread_ts": "1.0"},
            ),
            source_node="planner",
        )
        await node._send_external(msg)
        app.client.chat_postMessage.assert_awaited_once_with(
            channel="C-ALLOWED", text="reply", thread_ts="1.0"
        )

    async def test_missing_channel_drops_send(self, slack_gateway):
        node, app, _bus = slack_gateway
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(text="reply", channel="slack", metadata={}),
            source_node="planner",
        )
        await node._send_external(msg)
        app.client.chat_postMessage.assert_not_awaited()


# ── Telegram config ──────────────────────────────────────────────────────────


class TestTelegramConfig:
    def test_requires_token(self):
        with pytest.raises(Exception):
            TelegramConfig(bot_token="")

    def test_defaults(self):
        cfg = TelegramConfig(bot_token="abc")
        assert cfg.allowed_chats == []
        assert cfg.api_base.startswith("https://")
        assert cfg.long_poll_timeout_s == 25


# ── Telegram gateway with fake httpx client ──────────────────────────────────


@dataclass
class _FakeResponse:
    data: dict = field(default_factory=dict)
    status: int = 200

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self) -> dict:
        return self.data


class _FakeHTTPClient:
    def __init__(self):
        self.get_responses: list[_FakeResponse] = []
        self.posts: list[tuple[str, dict]] = []

    async def get(self, path, params=None):
        if not self.get_responses:
            return _FakeResponse(data={"ok": True, "result": []})
        return self.get_responses.pop(0)

    async def post(self, path, json=None):
        self.posts.append((path, json))
        return _FakeResponse(data={"ok": True, "result": {}})

    async def aclose(self) -> None:
        pass


@pytest.fixture
def tg_gateway():
    client = _FakeHTTPClient()
    node = TelegramGatewayNode(TelegramConfig(bot_token="abc:def"), client=client)
    bus = MagicMock()
    bus.publish = AsyncMock()
    node._bus = bus
    return node, client, bus


class TestTelegramInbound:
    async def test_fetch_advances_offset(self, tg_gateway):
        node, client, _bus = tg_gateway
        client.get_responses.append(
            _FakeResponse(
                data={
                    "ok": True,
                    "result": [{"update_id": 10, "message": {"text": "hi", "chat": {"id": 1}}}],
                }
            )
        )
        updates = await node._fetch_updates()
        assert len(updates) == 1
        assert node._offset == 11

    async def test_fetch_rejects_not_ok(self, tg_gateway):
        node, client, _bus = tg_gateway
        client.get_responses.append(_FakeResponse(data={"ok": False, "error": "bad"}))
        with pytest.raises(RuntimeError):
            await node._fetch_updates()

    async def test_dispatch_publishes_inbound(self, tg_gateway):
        node, _client, bus = tg_gateway
        await node._dispatch_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 42,
                    "text": "hello",
                    "chat": {"id": 555},
                    "from": {"id": 99, "username": "alice"},
                },
            }
        )
        bus.publish.assert_awaited()
        topic, payload = bus.publish.await_args.args
        assert topic == "/inbound"
        assert payload.sender == "alice"
        assert payload.metadata == {"chat_id": 555, "message_id": 42}

    async def test_dispatch_respects_allowlist(self, tg_gateway):
        node, _client, bus = tg_gateway
        node._config.allowed_chats = [111]
        await node._dispatch_update({"update_id": 1, "message": {"text": "x", "chat": {"id": 222}}})
        bus.publish.assert_not_awaited()

    async def test_dispatch_skips_non_message(self, tg_gateway):
        node, _client, bus = tg_gateway
        await node._dispatch_update({"update_id": 1, "callback_query": {}})
        bus.publish.assert_not_awaited()

    async def test_dispatch_skips_empty_text(self, tg_gateway):
        node, _client, bus = tg_gateway
        await node._dispatch_update({"update_id": 1, "message": {"chat": {"id": 1}}})
        bus.publish.assert_not_awaited()


class TestTelegramOutbound:
    async def test_send_uses_chat_id_from_metadata(self, tg_gateway):
        node, client, _bus = tg_gateway
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(
                text="reply",
                channel="telegram",
                metadata={"chat_id": 555, "message_id": 42},
            ),
            source_node="planner",
        )
        await node._send_external(msg)
        assert len(client.posts) == 1
        path, body = client.posts[0]
        assert path == "/sendMessage"
        assert body["chat_id"] == 555
        assert body["text"] == "reply"
        assert body["reply_to_message_id"] == 42

    async def test_missing_chat_id_drops(self, tg_gateway):
        node, client, _bus = tg_gateway
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(text="x", channel="telegram", metadata={}),
            source_node="planner",
        )
        await node._send_external(msg)
        assert client.posts == []


# ── ChannelStatus publishing ─────────────────────────────────────────────────


class TestChannelStatus:
    async def test_publish_channel_status_sends_to_topic(self, tg_gateway):
        node, _client, bus = tg_gateway
        await node.publish_channel_status("connected", detail="ok")
        bus.publish.assert_awaited()
        topic, payload = bus.publish.await_args.args
        assert topic == "/system/channels"
        assert isinstance(payload, ChannelStatus)
        assert payload.channel == "telegram"
        assert payload.state == "connected"
        assert payload.detail == "ok"

    async def test_publish_channel_status_noop_when_unbound(self):
        node = _FakeGateway(_FakeConfig(token="abc"))
        # No bus assigned — should silently no-op, not raise.
        await node.publish_channel_status("connected")


# ── Circuit breaker threshold ────────────────────────────────────────────────


class TestReconnectCircuitBreaker:
    def test_constant_is_five(self):
        # This is a product invariant — changing it affects operator
        # expectations and any alerting keyed on reconnect attempts.
        assert MAX_CONSECUTIVE_GATEWAY_FAILURES == 5


# ── ReconnectPolicy ──────────────────────────────────────────────────────────


class TestReconnectPolicy:
    def test_first_delay_near_initial(self):
        p = ReconnectPolicy(initial_s=2.0, max_s=30.0, factor=1.8, jitter=0.0)
        d = p.next_delay()
        assert d == pytest.approx(2.0, abs=0.001)
        assert p.attempts == 1

    def test_grows_exponentially(self):
        p = ReconnectPolicy(initial_s=1.0, max_s=100.0, factor=2.0, jitter=0.0)
        delays = [p.next_delay() for _ in range(4)]
        assert delays == pytest.approx([1.0, 2.0, 4.0, 8.0])

    def test_caps_at_max(self):
        p = ReconnectPolicy(initial_s=1.0, max_s=5.0, factor=10.0, jitter=0.0)
        p.next_delay()
        p.next_delay()
        assert p.next_delay() == pytest.approx(5.0)

    def test_jitter_bounds(self):
        p = ReconnectPolicy(initial_s=10.0, max_s=100.0, factor=1.0, jitter=0.25)
        for _ in range(50):
            d = p.next_delay()
            # jitter=0.25 means [7.5, 12.5]
            assert 7.5 - 1e-6 <= d <= 12.5 + 1e-6

    def test_reset_clears_attempts(self):
        p = ReconnectPolicy()
        p.next_delay()
        p.next_delay()
        p.reset()
        assert p.attempts == 0

    def test_exhausted_flag(self):
        p = ReconnectPolicy(max_attempts=2, jitter=0.0)
        assert not p.exhausted
        p.next_delay()
        p.next_delay()
        assert p.exhausted


# ── chunk_text ───────────────────────────────────────────────────────────────


class TestChunkText:
    def test_empty_returns_empty_list(self):
        assert chunk_text("", 100) == []

    def test_short_returns_single(self):
        assert chunk_text("hi", 100) == ["hi"]

    def test_exact_limit_returns_single(self):
        s = "a" * 100
        assert chunk_text(s, 100) == [s]

    def test_prefers_paragraph_break(self):
        s = "para1\n\npara2\n\npara3"
        out = chunk_text(s, 12)
        assert out[0] == "para1"
        assert "para2" in out[1]

    def test_falls_back_to_line_break(self):
        s = "line1\nline2\nline3"
        out = chunk_text(s, 10)
        assert out[0] == "line1"

    def test_falls_back_to_space(self):
        s = "word1 word2 word3 word4"
        out = chunk_text(s, 12)
        # Should cut at a space, not mid-word.
        for piece in out:
            assert " " not in piece or all(
                not piece.startswith(" ") and not piece.endswith(" ") for _ in [None]
            )
        assert "".join(p + " " for p in out).replace("  ", " ").strip() == s.strip().replace(
            "  ", " "
        )

    def test_hard_chop_for_long_unbreakable(self):
        s = "a" * 250
        out = chunk_text(s, 100)
        assert len(out) == 3
        assert all(len(p) <= 100 for p in out)

    def test_slack_constant_is_8000(self):
        assert SLACK_TEXT_LIMIT == 8000

    def test_telegram_constant_is_4096(self):
        assert TELEGRAM_TEXT_LIMIT == 4096


# ── DedupCache ───────────────────────────────────────────────────────────────


class TestDedupCache:
    def test_requires_positive_capacity(self):
        with pytest.raises(ValueError):
            DedupCache(capacity=0)

    def test_first_insert_is_new(self):
        c = DedupCache()
        assert c.check_and_add("a") is False
        assert "a" in c

    def test_second_insert_is_duplicate(self):
        c = DedupCache()
        c.check_and_add("a")
        assert c.check_and_add("a") is True

    def test_lru_eviction(self):
        c = DedupCache(capacity=3)
        c.check_and_add("a")
        c.check_and_add("b")
        c.check_and_add("c")
        c.check_and_add("d")  # evicts "a"
        assert "a" not in c
        assert "d" in c
        assert len(c) == 3

    def test_hit_refreshes_lru_position(self):
        c = DedupCache(capacity=3)
        c.check_and_add("a")
        c.check_and_add("b")
        c.check_and_add("c")
        c.check_and_add("a")  # move-to-end
        c.check_and_add("d")  # evicts "b" (oldest after refresh)
        assert "a" in c
        assert "b" not in c

    def test_non_str_membership_is_false(self):
        c = DedupCache()
        c.check_and_add("a")
        assert 42 not in c


# ── StallWatchdog ────────────────────────────────────────────────────────────


class TestStallWatchdog:
    def test_rejects_zero_idle(self):
        with pytest.raises(ValueError):
            StallWatchdog(idle_s=0, on_stall=lambda: None)  # type: ignore[arg-type]

    async def test_fires_on_idle(self):
        import asyncio as _aio

        fired = _aio.Event()

        async def _on_stall() -> None:
            fired.set()

        w = StallWatchdog(idle_s=0.05, on_stall=_on_stall, check_interval_s=0.01)
        w.start()
        try:
            await _aio.wait_for(fired.wait(), timeout=1.0)
        finally:
            await w.stop()
        assert w.fired

    async def test_heartbeat_prevents_fire(self):
        import asyncio as _aio

        fired = _aio.Event()

        async def _on_stall() -> None:
            fired.set()

        w = StallWatchdog(idle_s=0.1, on_stall=_on_stall, check_interval_s=0.01)
        w.start()
        try:
            for _ in range(5):
                await _aio.sleep(0.03)
                w.heartbeat()
            # If heartbeats were honored we should NOT have fired yet.
            assert not fired.is_set()
        finally:
            await w.stop()


# ── Slack self-echo cache & dedup ────────────────────────────────────────────


class TestSlackDedupAndEcho:
    async def test_duplicate_ts_dropped(self, slack_gateway):
        node, _app, bus = slack_gateway
        evt = {"user": "U-ALLOWED", "channel": "C-ALLOWED", "text": "hi", "ts": "1.0"}
        await node._handle_event(evt)
        await node._handle_event(evt)
        assert bus.publish.await_count == 1

    async def test_own_ts_dropped(self, slack_gateway):
        node, _app, bus = slack_gateway
        node._own_ts.add("9.9")
        await node._handle_event(
            {"user": "U-ALLOWED", "channel": "C-ALLOWED", "text": "hi", "ts": "9.9"}
        )
        bus.publish.assert_not_awaited()

    async def test_postmessage_records_ts(self, slack_gateway):
        node, app, _bus = slack_gateway
        app.client.chat_postMessage.return_value = {"ok": True, "ts": "42.0"}
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(
                text="reply",
                channel="slack",
                metadata={"slack_channel": "C-ALLOWED", "thread_ts": "1.0"},
            ),
            source_node="planner",
        )
        await node._send_external(msg)
        assert "42.0" in node._own_ts


class TestSlackChunking:
    async def test_long_text_is_chunked(self, slack_gateway):
        node, app, _bus = slack_gateway
        app.client.chat_postMessage.return_value = {"ok": True, "ts": "1"}
        long = ("word " * 5000).strip()  # > 8000 chars
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(
                text=long,
                channel="slack",
                metadata={"slack_channel": "C-ALLOWED"},
            ),
            source_node="planner",
        )
        await node._send_external(msg)
        # 5000 * 5 = 25000 chars → ceil(25000 / 8000) = 4 chunks.
        assert app.client.chat_postMessage.await_count >= 3


# ── Telegram dedup, chunking, auth short-circuit ─────────────────────────────


class TestTelegramDedup:
    async def test_duplicate_update_id_dropped(self, tg_gateway):
        node, _client, bus = tg_gateway
        upd = {
            "update_id": 7,
            "message": {"text": "hi", "chat": {"id": 1}, "message_id": 1},
        }
        await node._dispatch_update(upd)
        await node._dispatch_update(upd)
        assert bus.publish.await_count == 1


class TestTelegramChunking:
    async def test_long_text_multiple_sends(self, tg_gateway):
        node, client, _bus = tg_gateway
        long = ("word " * 3000).strip()  # 15000 chars > 4096
        msg = Message(
            topic="/outbound",
            payload=OutboundChat(
                text=long,
                channel="telegram",
                metadata={"chat_id": 1, "message_id": 42},
            ),
            source_node="planner",
        )
        await node._send_external(msg)
        assert len(client.posts) >= 3
        # First chunk includes reply_to_message_id, rest don't.
        first_body = client.posts[0][1]
        assert first_body.get("reply_to_message_id") == 42
        for _, body in client.posts[1:]:
            assert "reply_to_message_id" not in body


class TestTelegramAuthShortCircuit:
    async def test_non_recoverable_error_stops_listener(self, tg_gateway, monkeypatch):
        node, _client, bus = tg_gateway

        # First fetch raises 401 Unauthorized — listener should publish error and return.
        calls = {"n": 0}

        async def _fetch():
            calls["n"] += 1
            raise RuntimeError("HTTP 401 Unauthorized")

        monkeypatch.setattr(node, "_fetch_updates", _fetch)
        await node._listen_external()
        assert calls["n"] == 1
        # Look for an error ChannelStatus among publishes.
        error_published = any(
            call.args[0] == "/system/channels" and call.args[1].state == "error"
            for call in bus.publish.await_args_list
        )
        assert error_published


# ── Plugin probe ─────────────────────────────────────────────────────────────


class TestSlackProbe:
    async def test_probe_ok_on_auth_test(self, monkeypatch):
        from agentbus.channels.slack import SlackPlugin

        class _FakeClient:
            def __init__(self, token):
                self.token = token

            async def auth_test(self):
                return {"ok": True, "team": "T1", "user": "bot"}

        class _FakeModule:
            AsyncWebClient = _FakeClient

        import sys

        fake_pkg = type(sys)("slack_sdk")
        fake_web = type(sys)("slack_sdk.web")
        fake_web_async = _FakeModule
        monkeypatch.setitem(sys.modules, "slack_sdk", fake_pkg)
        monkeypatch.setitem(sys.modules, "slack_sdk.web", fake_web)
        monkeypatch.setitem(sys.modules, "slack_sdk.web.async_client", fake_web_async)

        cfg = SlackConfig(app_token="xapp-1", bot_token="xoxb-1")
        result = await SlackPlugin.probe(cfg)
        assert result.status == "ok"
        assert "T1" in result.detail

    async def test_probe_fail_on_exception(self, monkeypatch):
        from agentbus.channels.slack import SlackPlugin

        class _Boom:
            def __init__(self, token):
                pass

            async def auth_test(self):
                raise RuntimeError("invalid_auth")

        class _FakeModule:
            AsyncWebClient = _Boom

        import sys

        fake_pkg = type(sys)("slack_sdk")
        fake_web = type(sys)("slack_sdk.web")
        fake_web_async = _FakeModule
        monkeypatch.setitem(sys.modules, "slack_sdk", fake_pkg)
        monkeypatch.setitem(sys.modules, "slack_sdk.web", fake_web)
        monkeypatch.setitem(sys.modules, "slack_sdk.web.async_client", fake_web_async)

        cfg = SlackConfig(app_token="xapp-1", bot_token="xoxb-1")
        result = await SlackPlugin.probe(cfg)
        assert result.status == "fail"
        assert "invalid_auth" in result.detail


class TestTelegramProbe:
    async def test_probe_ok_on_getme(self, monkeypatch):
        from agentbus.channels.telegram import TelegramPlugin

        class _Resp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url):
                return _Resp({"ok": True, "result": {"username": "testbot"}})

        import httpx as _real_httpx

        monkeypatch.setattr(_real_httpx, "AsyncClient", _Client)
        cfg = TelegramConfig(bot_token="abc:def")
        result = await TelegramPlugin.probe(cfg)
        assert result.status == "ok"
        assert "testbot" in result.detail
