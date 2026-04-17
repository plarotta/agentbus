"""Pydantic model for the ``channels.slack`` block of ``agentbus.yaml``."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SlackConfig(BaseModel):
    """Slack Socket Mode gateway config.

    ``allowed_channels`` and ``allowed_senders`` are optional inbound
    allowlists. Empty list = allow all. Values are Slack IDs
    (``C01234…`` for channels, ``U01234…`` for users), not display
    names, because display names are mutable and not unique.
    """

    app_token: str = Field(..., min_length=1, description="xapp-… app-level token")
    bot_token: str = Field(..., min_length=1, description="xoxb-… bot token")
    allowed_channels: list[str] = Field(default_factory=list)
    allowed_senders: list[str] = Field(default_factory=list)
    ignore_bots: bool = True
