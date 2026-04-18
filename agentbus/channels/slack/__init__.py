"""Slack channel plugin.

Uses Socket Mode via ``slack-bolt`` so agentbus can run behind NAT without
exposing a public HTTPS endpoint. Requires both:

* an **app-level token** (``xapp-…``, scope ``connections:write``) for
  the WebSocket connection, and
* a **bot token** (``xoxb-…``) for outbound API calls.

Install the optional extra::

    uv sync --extra slack
"""

from __future__ import annotations

from typing import ClassVar

from agentbus.channels.base import ChannelPlugin, ProbeResult
from agentbus.channels.loader import register_plugin
from agentbus.gateway import GatewayNode

from .config import SlackConfig


class SlackPlugin(ChannelPlugin[SlackConfig]):
    name: ClassVar[str] = "slack"
    ConfigModel: ClassVar[type[SlackConfig]] = SlackConfig

    @classmethod
    async def probe(cls, config: SlackConfig) -> ProbeResult:
        """Call Slack's ``auth.test`` with the configured bot token.

        Cheap, well-defined, and the single best signal that the token
        is still valid. Returns ``fail`` on auth errors, ``warn`` if
        slack-sdk isn't installed (the embedder may still be using a
        custom build), ``ok`` otherwise.
        """
        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError:
            return ProbeResult(status="warn", detail="slack-sdk not installed")
        client = AsyncWebClient(token=config.bot_token)
        try:
            resp = await client.auth_test()
            team = resp.get("team") or resp.get("team_id") or "unknown"
            user = resp.get("user") or resp.get("user_id") or "unknown"
            return ProbeResult(status="ok", detail=f"team={team} bot={user}")
        except Exception as exc:
            return ProbeResult(status="fail", detail=str(exc))

    @classmethod
    def setup_wizard(cls, existing: dict | None = None) -> SlackConfig:
        """Prompt for Slack tokens via stdin. Used by ``agentbus channels setup slack``."""
        existing = existing or {}
        print("Slack channel setup.")
        print("  App-level token (xapp-…, scope connections:write)")
        print("  Bot token       (xoxb-…, from OAuth & Permissions)\n")
        app_token = input(f"App token [{_mask(existing.get('app_token'))}]: ").strip()
        bot_token = input(f"Bot token [{_mask(existing.get('bot_token'))}]: ").strip()
        return SlackConfig(
            app_token=app_token or existing.get("app_token", ""),
            bot_token=bot_token or existing.get("bot_token", ""),
            allowed_channels=list(existing.get("allowed_channels", []) or []),
            allowed_senders=list(existing.get("allowed_senders", []) or []),
        )

    @classmethod
    def create_gateway(cls, config: SlackConfig) -> GatewayNode:
        from .gateway import SlackGatewayNode

        return SlackGatewayNode(config)


def _mask(token: str | None) -> str:
    if not token:
        return ""
    if len(token) < 8:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


register_plugin(SlackPlugin)

__all__ = ["SlackConfig", "SlackPlugin"]
