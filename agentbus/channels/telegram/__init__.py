"""Telegram channel plugin.

Uses the Telegram Bot API's long-poll ``getUpdates`` endpoint via raw
``httpx`` — no new dependency beyond the existing ``ollama`` extra. A
single bot token is enough; no webhooks, no public URL.

Install the optional extra::

    uv sync --extra telegram
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from agentbus.channels.base import ChannelPlugin, ProbeResult
from agentbus.channels.loader import register_plugin
from agentbus.gateway import GatewayNode

from .config import TelegramConfig

if TYPE_CHECKING:
    from agentbus.setup.prompter import Prompter


class TelegramPlugin(ChannelPlugin[TelegramConfig]):
    name: ClassVar[str] = "telegram"
    ConfigModel: ClassVar[type[TelegramConfig]] = TelegramConfig

    @classmethod
    async def probe(cls, config: TelegramConfig) -> ProbeResult:
        """Call Telegram's ``getMe`` — the canonical cheap auth check.

        Returns ``fail`` on 401 / network errors, ``warn`` if httpx
        isn't installed, ``ok`` otherwise (with the bot username in
        the detail so the operator can confirm the right account).
        """
        try:
            import httpx
        except ImportError:
            return ProbeResult(status="warn", detail="httpx not installed")
        url = f"{config.api_base.rstrip('/')}/bot{config.bot_token}/getMe"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            return ProbeResult(status="fail", detail=str(exc))
        if not data.get("ok"):
            return ProbeResult(status="fail", detail=str(data))
        result = data.get("result") or {}
        username = result.get("username") or "unknown"
        return ProbeResult(status="ok", detail=f"@{username}")

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
    def interactive_setup(
        cls,
        prompter: Prompter,
        existing: dict | None = None,
    ) -> TelegramConfig:
        """Themed setup for ``agentbus setup`` — same shape as Slack's."""
        existing = existing or {}
        prompter.note(
            "Telegram needs a bot token from @BotFather → /newbot.",
            tone="muted",
        )
        mask = _mask(existing.get("bot_token")) or "(none)"
        bot_token = prompter.password(
            f"Bot token  [current: {mask}]",
            default=existing.get("bot_token"),
        )
        allowed_chats_raw = prompter.text(
            "Allowed chat IDs (comma-separated ints, blank = allow all)",
            default=",".join(str(x) for x in (existing.get("allowed_chats") or [])),
            validate=_validate_int_csv,
        )
        allowed_chats = [int(x.strip()) for x in allowed_chats_raw.split(",") if x.strip()]
        return TelegramConfig(
            bot_token=bot_token or existing.get("bot_token", ""),
            allowed_chats=allowed_chats,
            api_base=existing.get("api_base") or "https://api.telegram.org",
            long_poll_timeout_s=int(existing.get("long_poll_timeout_s") or 25),
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


def _validate_int_csv(raw: str) -> str | None:
    if not raw.strip():
        return None
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        try:
            int(chunk)
        except ValueError:
            return f"not a valid integer: {chunk!r}"
    return None


register_plugin(TelegramPlugin)

__all__ = ["TelegramConfig", "TelegramPlugin"]
