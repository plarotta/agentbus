"""Slack gateway implementation.

The heavy lifting — WebSocket connection, event dispatch, retry — is
handled by ``slack-bolt``'s ``AsyncSocketModeHandler``. This module just
wires it into the agentbus bus:

* ``_listen_external`` starts the Socket Mode handler inside a
  circuit-breaker-guarded reconnect loop and translates ``message`` /
  ``app_mention`` events into :class:`InboundChat`.
* ``_send_external`` maps :class:`OutboundChat` back to
  ``chat.postMessage``, preserving the ``thread_ts`` carried through
  ``OutboundChat.metadata``.

``slack-bolt`` and ``slack-sdk`` are optional deps (``uv sync --extra
slack``). Importing this module without them installed raises
:class:`ChannelRuntimeError` at construction time with an actionable
message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

from agentbus.channels.base import (
    MAX_CONSECUTIVE_GATEWAY_FAILURES,
    ChannelRuntimeError,
)
from agentbus.gateway import GatewayNode
from agentbus.message import Message
from agentbus.node import BusHandle
from agentbus.schemas.common import InboundChat, OutboundChat
from agentbus.utils import CircuitBreaker

from .config import SlackConfig

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_SECONDS = 5.0


def _require_slack_sdk() -> tuple[Any, Any]:
    """Import slack-bolt lazily and raise a clear error if missing."""
    try:
        from slack_bolt.adapter.socket_mode.async_handler import (
            AsyncSocketModeHandler,
        )
        from slack_bolt.async_app import AsyncApp
    except ImportError as exc:  # pragma: no cover - import-error path
        raise ChannelRuntimeError(
            "Slack channel requires 'slack-bolt' — install with: uv sync --extra slack"
        ) from exc
    return AsyncApp, AsyncSocketModeHandler


class SlackGatewayNode(GatewayNode):
    """Socket Mode gateway. Name is ``slack-gateway`` for introspection clarity."""

    name = "slack-gateway"
    channel_name: ClassVar[str] = "slack"

    def __init__(self, config: SlackConfig) -> None:
        super().__init__()
        self._config = config
        self._app: Any = None
        self._handler: Any = None
        # Lazy import so the SDK isn't needed until a gateway is actually created.
        _require_slack_sdk()

    async def on_init(self, bus: BusHandle) -> None:
        self._bus = bus
        await self.publish_channel_status("starting")
        AsyncApp, AsyncSocketModeHandler = _require_slack_sdk()
        self._app = AsyncApp(token=self._config.bot_token)
        self._handler = AsyncSocketModeHandler(self._app, self._config.app_token)
        self._app.event("message")(self._handle_event)
        self._app.event("app_mention")(self._handle_event)
        self._listener_task = asyncio.create_task(self._listen_external())

    async def _handle_event(self, event: dict, **_: Any) -> None:
        """Callback invoked by slack-bolt for every ``message`` / ``app_mention`` event."""
        if self._config.ignore_bots and event.get("bot_id"):
            return
        # Slack sends many non-user message subtypes (channel_join, message_deleted,
        # etc.). We only care about plain user-authored text.
        if event.get("subtype"):
            return
        user = event.get("user") or event.get("username") or "unknown"
        channel_id = event.get("channel") or ""
        text = event.get("text") or ""
        if not text:
            return
        if self._config.allowed_channels and channel_id not in self._config.allowed_channels:
            return
        if self._config.allowed_senders and user not in self._config.allowed_senders:
            return
        thread_ts = event.get("thread_ts") or event.get("ts")
        await self.publish_external(
            InboundChat(
                channel="slack",
                sender=user,
                text=text,
                metadata={
                    "slack_channel": channel_id,
                    "thread_ts": thread_ts,
                    "ts": event.get("ts"),
                },
            )
        )

    async def _listen_external(self) -> None:
        """Run Socket Mode with a circuit-breaker-guarded reconnect loop."""
        breaker = CircuitBreaker("slack-gateway", max_failures=MAX_CONSECUTIVE_GATEWAY_FAILURES)
        while True:
            try:
                await self.publish_channel_status("connected")
                await self._handler.start_async()
                # start_async returns only on clean disconnect; treat as transient.
                breaker.record_success()
                await self.publish_channel_status("reconnecting", detail="socket disconnected")
            except asyncio.CancelledError:
                await self.publish_channel_status("stopped")
                raise
            except Exception as exc:
                logger.warning("Slack socket error: %s", exc)
                tripped = breaker.record_failure()
                if tripped:
                    await self.publish_channel_status("error", detail=str(exc))
                    return
                await self.publish_channel_status("reconnecting", detail=str(exc))
            await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)

    async def _send_external(self, msg: Message) -> None:
        payload = msg.payload
        if not isinstance(payload, OutboundChat) or self._app is None:
            return
        slack_channel = payload.metadata.get("slack_channel")
        if not slack_channel:
            logger.warning("Slack outbound dropped — missing slack_channel metadata")
            return
        try:
            await self._app.client.chat_postMessage(
                channel=slack_channel,
                text=payload.text,
                thread_ts=payload.metadata.get("thread_ts"),
            )
        except Exception as exc:
            logger.warning("Slack chat_postMessage failed: %s", exc)

    async def on_shutdown(self) -> None:
        await super().on_shutdown()
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception as exc:  # pragma: no cover - cleanup best-effort
                logger.debug("Slack handler close error: %s", exc)
        await self.publish_channel_status("stopped")
