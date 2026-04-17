"""Telegram gateway implementation (raw ``getUpdates`` long-poll).

Keeps dependencies minimal by skipping ``python-telegram-bot``. The
long-poll loop calls ``getUpdates`` with a configurable timeout and an
ever-advancing ``offset`` (Telegram's way of acknowledging received
updates — "next call, give me everything > offset"). We translate each
inbound ``message`` update into :class:`InboundChat`, and
:class:`OutboundChat` back into ``sendMessage``.

Reconnect behaviour: transient HTTP errors, timeouts, and 5xx responses
fall into a :class:`~agentbus.utils.CircuitBreaker`-guarded reconnect
loop. After ``MAX_CONSECUTIVE_GATEWAY_FAILURES`` consecutive failures
the gateway publishes an ``error`` :class:`ChannelStatus` and stops —
stops retrying to avoid hammering a dead token or a revoked bot.

``httpx`` is already the transitive dep for the ``ollama`` embedding
provider, so no new dependency is strictly required. A ``telegram``
extra is still published for clarity.
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

from .config import TelegramConfig

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_SECONDS = 5.0


def _require_httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise ChannelRuntimeError(
            "Telegram channel requires 'httpx' — install with: uv sync --extra telegram"
        ) from exc
    return httpx


class TelegramGatewayNode(GatewayNode):
    name = "telegram-gateway"
    channel_name: ClassVar[str] = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        *,
        client: Any = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._offset = 0
        self._client = client  # tests inject a fake httpx-shaped client
        self._owns_client = client is None

    async def on_init(self, bus: BusHandle) -> None:
        self._bus = bus
        if self._client is None:
            httpx = _require_httpx()
            self._client = httpx.AsyncClient(
                base_url=self._endpoint_base(),
                timeout=self._config.long_poll_timeout_s + 10,
            )
        await self.publish_channel_status("starting")
        self._listener_task = asyncio.create_task(self._listen_external())

    def _endpoint_base(self) -> str:
        return f"{self._config.api_base.rstrip('/')}/bot{self._config.bot_token}"

    async def _listen_external(self) -> None:
        breaker = CircuitBreaker(
            "telegram-gateway", max_failures=MAX_CONSECUTIVE_GATEWAY_FAILURES
        )
        await self.publish_channel_status("connected")
        while True:
            try:
                updates = await self._fetch_updates()
                breaker.record_success()
                for update in updates:
                    await self._dispatch_update(update)
            except asyncio.CancelledError:
                await self.publish_channel_status("stopped")
                raise
            except Exception as exc:
                logger.warning("Telegram long-poll error: %s", exc)
                tripped = breaker.record_failure()
                if tripped:
                    await self.publish_channel_status("error", detail=str(exc))
                    return
                await self.publish_channel_status("reconnecting", detail=str(exc))
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)

    async def _fetch_updates(self) -> list[dict]:
        assert self._client is not None
        params = {
            "timeout": self._config.long_poll_timeout_s,
            "offset": self._offset,
        }
        resp = await self._client.get("/getUpdates", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getUpdates not ok: {data}")
        updates = data.get("result") or []
        if updates:
            # Acknowledge by advancing offset past the highest update_id.
            self._offset = max(int(u["update_id"]) for u in updates) + 1
        return list(updates)

    async def _dispatch_update(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        if self._config.allowed_chats and chat_id not in self._config.allowed_chats:
            return
        text = message.get("text")
        if not text:
            return
        sender = (message.get("from") or {}).get("username") or str(
            (message.get("from") or {}).get("id", "unknown")
        )
        await self.publish_external(
            InboundChat(
                channel="telegram",
                sender=sender,
                text=text,
                metadata={
                    "chat_id": chat_id,
                    "message_id": message.get("message_id"),
                },
            )
        )

    async def _send_external(self, msg: Message) -> None:
        payload = msg.payload
        if not isinstance(payload, OutboundChat) or self._client is None:
            return
        chat_id = payload.metadata.get("chat_id")
        if chat_id is None:
            logger.warning("Telegram outbound dropped — missing chat_id metadata")
            return
        body: dict[str, Any] = {"chat_id": chat_id, "text": payload.text}
        reply_to_mid = payload.metadata.get("message_id")
        if reply_to_mid is not None:
            body["reply_to_message_id"] = reply_to_mid
        try:
            resp = await self._client.post("/sendMessage", json=body)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Telegram sendMessage failed: %s", exc)

    async def on_shutdown(self) -> None:
        await super().on_shutdown()
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # pragma: no cover
                logger.debug("Telegram client close error: %s", exc)
        await self.publish_channel_status("stopped")
