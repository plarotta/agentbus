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
from agentbus.channels.slack.config import SlackConfig
from agentbus.channels.telegram.config import TelegramConfig
from agentbus.channels.telegram.gateway import TelegramGatewayNode
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
