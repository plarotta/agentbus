"""Channel plugin registry + YAML config loader.

``agentbus.yaml`` exposes a single ``channels:`` block. Each key is a
plugin name and the value is the plugin's config block:

.. code-block:: yaml

    channels:
      slack:
        enabled: true
        app_token: ${SLACK_APP_TOKEN}
        bot_token: ${SLACK_BOT_TOKEN}
        allowed_channels: ["C01234"]
      telegram:
        enabled: true
        bot_token: ${TELEGRAM_BOT_TOKEN}
        allowed_chats: [12345]

:func:`load_channels_from_dict` validates the block against each plugin's
``ConfigModel`` and returns a list of ``(plugin, validated_config)``
pairs. :func:`open_channels_runtime` then instantiates one
:class:`~agentbus.gateway.GatewayNode` per plugin and returns a
:class:`ChannelsRuntime` that the launch path owns for the duration of
the bus run.

Plugins register themselves at import time (see each subpackage's
``__init__.py``). :func:`register_plugin` is the entry point;
:func:`registered_plugins` returns the current registry for inspection
or testing.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from agentbus.gateway import GatewayNode

from .base import ChannelPlugin, ChannelRuntimeError

logger = logging.getLogger(__name__)

_BUILTIN_PLUGIN_MODULES = {
    "slack": "agentbus.channels.slack",
    "telegram": "agentbus.channels.telegram",
}

_REGISTRY: dict[str, type[ChannelPlugin]] = {}


def register_plugin(plugin: type[ChannelPlugin]) -> None:
    """Add ``plugin`` to the process-wide registry. Idempotent — re-registering
    the same class is a no-op, registering a *different* class under the same
    name is an error (catches accidental name collisions)."""
    existing = _REGISTRY.get(plugin.name)
    if existing is plugin:
        return
    if existing is not None:
        raise ChannelRuntimeError(
            f"Channel plugin name conflict: {plugin.name!r} already registered as {existing!r}"
        )
    _REGISTRY[plugin.name] = plugin


def registered_plugins() -> dict[str, type[ChannelPlugin]]:
    """Snapshot of the current registry. Mostly useful for tests and the CLI."""
    return dict(_REGISTRY)


def _ensure_plugin_imported(name: str) -> None:
    """Best-effort: if a config names a builtin plugin we haven't loaded yet,
    import its subpackage so the ``register_plugin`` side effect fires. Third-
    party plugins must be imported by the embedder before :func:`load_channels_from_dict`
    is called."""
    if name in _REGISTRY:
        return
    module_path = _BUILTIN_PLUGIN_MODULES.get(name)
    if module_path is None:
        return
    try:
        importlib.import_module(module_path)
    except ChannelRuntimeError:
        # Plugin module raised explicitly at import (missing SDK, etc.) —
        # let it bubble so the user sees the real cause.
        raise
    except ImportError as exc:
        raise ChannelRuntimeError(
            f"Channel {name!r} requires optional dependencies that are not installed: {exc}"
        ) from exc


def load_channels_from_dict(
    data: Any,
) -> list[tuple[type[ChannelPlugin], BaseModel]]:
    """Parse the ``channels:`` mapping from ``agentbus.yaml``.

    Returns a list of ``(plugin_cls, validated_config)`` pairs, one per
    enabled channel. Channels with ``enabled: false`` (or an explicit
    ``false`` value) are skipped silently; unknown plugin names raise
    :class:`ChannelRuntimeError`. Validation errors are surfaced the
    same way — with the channel name prefixed so the user can find the
    broken block in YAML quickly.
    """
    if not data:
        return []
    if not isinstance(data, dict):
        raise ChannelRuntimeError("channels: must be a mapping of {name: config}")

    out: list[tuple[type[ChannelPlugin], BaseModel]] = []
    for name, block in data.items():
        if block is False:
            continue
        if isinstance(block, dict) and block.get("enabled") is False:
            continue
        block_dict: dict[str, Any] = dict(block) if isinstance(block, dict) else {}
        block_dict.pop("enabled", None)
        _ensure_plugin_imported(name)
        plugin_cls = _REGISTRY.get(name)
        if plugin_cls is None:
            known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
            raise ChannelRuntimeError(f"Unknown channel plugin {name!r}. Known plugins: {known}")
        try:
            validated = plugin_cls.ConfigModel(**block_dict)
        except ValidationError as exc:
            raise ChannelRuntimeError(f"Invalid config for channel {name!r}: {exc}") from exc
        out.append((plugin_cls, validated))
    return out


@dataclass
class ChannelsRuntime:
    """Holds the instantiated gateway nodes for a running bus.

    Lifecycle mirrors :class:`agentbus.mcp.MCPRuntime` / :class:`agentbus.memory.MemoryRuntime`:
    the caller (launch path) owns open + close. :meth:`close` is a no-op
    today — gateways run their own listener tasks and clean them up in
    ``on_shutdown``. The method exists so embedders can adopt the same
    ``async with`` shape as the other runtimes without special-casing.
    """

    nodes: list[GatewayNode] = field(default_factory=list)

    async def aclose(self) -> None:
        # Gateways shut down via Node.on_shutdown when the bus stops.
        return None


async def open_channels_runtime(
    configs: list[tuple[type[ChannelPlugin], BaseModel]],
) -> ChannelsRuntime:
    """Instantiate a :class:`GatewayNode` for each ``(plugin, config)`` pair.

    Failures per-channel are *not* swallowed here — construction should
    be cheap and deterministic (no network I/O until the bus spins up).
    If a channel's construction raises, the whole runtime fails; the
    launcher decides whether to log + continue or abort.
    """
    runtime = ChannelsRuntime()
    for plugin_cls, config in configs:
        node = plugin_cls.create_gateway(config)
        runtime.nodes.append(node)
    return runtime
