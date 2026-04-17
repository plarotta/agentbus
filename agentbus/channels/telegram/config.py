"""Pydantic model for the ``channels.telegram`` block of ``agentbus.yaml``."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TelegramConfig(BaseModel):
    """Telegram Bot API gateway config.

    ``allowed_chats`` is an optional list of chat IDs (ints — Telegram
    uses negative IDs for group chats, positive for private DMs). Empty
    list = allow every chat the bot is in.
    """

    bot_token: str = Field(..., min_length=1, description="Telegram bot token from @BotFather")
    allowed_chats: list[int] = Field(default_factory=list)
    api_base: str = "https://api.telegram.org"
    long_poll_timeout_s: int = Field(default=25, ge=1, le=60)
