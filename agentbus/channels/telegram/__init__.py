"""Telegram channel plugin.

Uses the Telegram Bot API's long-poll ``getUpdates`` endpoint via raw
``httpx`` — no new dependency beyond the existing ``ollama`` extra. A
single bot token is enough; no webhooks, no public URL.

Install the optional extra::

    uv sync --extra telegram
"""

from __future__ import annotations

from typing import ClassVar

from agentbus.channels.base import ChannelPlugin
from agentbus.channels.loader import register_plugin
from agentbus.gateway import GatewayNode

from .config import TelegramConfig


class TelegramPlugin(ChannelPlugin[TelegramConfig]):
    name: ClassVar[str] = "telegram"
    ConfigModel: ClassVar[type[TelegramConfig]] = TelegramConfig

    @classmethod
    def setup_wizard(cls, existing: dict | None = None) -> TelegramConfig:
        existing = existing or {}
        print("Telegram channel setup.")
        print("  Bot token: obtain via @BotFather → /newbot\n")
        bot_token = input(f"Bot token [{_mask(existing.get('bot_token'))}]: ").strip()
        allowed_raw = input("Allowed chat IDs (comma-separated, blank = allow all): ").strip()
        allowed = (
            [int(x.strip()) for x in allowed_raw.split(",") if x.strip()]
            if allowed_raw
            else list(existing.get("allowed_chats", []) or [])
        )
        return TelegramConfig(
            bot_token=bot_token or existing.get("bot_token", ""),
            allowed_chats=allowed,
        )

    @classmethod
    def create_gateway(cls, config: TelegramConfig) -> GatewayNode:
        from .gateway import TelegramGatewayNode

        return TelegramGatewayNode(config)


def _mask(token: str | None) -> str:
    if not token:
        return ""
    if len(token) < 8:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


register_plugin(TelegramPlugin)

__all__ = ["TelegramConfig", "TelegramPlugin"]
