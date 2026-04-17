import asyncio
import importlib
import logging
from pathlib import Path
from typing import Any

import yaml

from agentbus.bus import MessageBus
from agentbus.topic import Topic

logger = logging.getLogger(__name__)


def _load_yaml_or_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text)


def import_string(path: str) -> Any:
    if ":" in path:
        module_name, attr = path.split(":", 1)
    else:
        module_name, attr = path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def build_bus_from_config(config: dict[str, Any]) -> MessageBus:
    bus_config = config.get("bus", {})
    global_retention = bus_config.get("global_retention", 0)
    bus = MessageBus(
        heartbeat_interval=bus_config.get("heartbeat_interval", 30.0),
        socket_path=bus_config.get("introspection_socket", "/tmp/agentbus.sock"),
    )

    for topic_config in config.get("topics", []):
        schema = import_string(topic_config["schema"])
        retention = topic_config.get("retention", global_retention)
        topic = Topic[schema](
            topic_config["name"],
            retention=retention,
            description=topic_config.get("description", ""),
        )
        bus.register_topic(topic)

    for node_config in config.get("nodes", []):
        node_cls = import_string(node_config["class"])
        kwargs = dict(node_config.get("config", {}))
        node = node_cls(**kwargs)
        if "concurrency" in node_config:
            node.concurrency = node_config["concurrency"]
        bus.register_node(node)

    _register_channels(bus, config.get("channels"))

    return bus


def _register_channels(bus: MessageBus, channels_config: Any) -> None:
    """Resolve the ``channels:`` block and register one GatewayNode per enabled channel.

    Per-channel construction failures are logged and skipped rather than aborting
    the bus — one mis-configured plugin shouldn't take down the whole deployment.
    A fully malformed ``channels:`` block (not a mapping) is still a hard error.
    """
    if not channels_config:
        return
    from agentbus.channels import (
        ChannelRuntimeError,
        load_channels_from_dict,
    )

    configs = load_channels_from_dict(channels_config)
    for plugin_cls, validated in configs:
        try:
            node = plugin_cls.create_gateway(validated)
        except ChannelRuntimeError as exc:
            logger.warning("Skipping channel %s: %s", plugin_cls.name, exc)
            continue
        bus.register_node(node)


async def launch(config_path: str | Path):
    config = _load_yaml_or_json(Path(config_path))
    bus = build_bus_from_config(config)
    shutdown_cfg = config.get("bus", {}).get("shutdown", {})
    return await bus.spin(
        drain_timeout=shutdown_cfg.get("drain_timeout", 5.0),
        install_signal_handlers=shutdown_cfg.get("install_signal_handlers", True),
    )


def launch_sync(config_path: str | Path):
    return asyncio.run(launch(config_path))


__all__ = ["build_bus_from_config", "import_string", "launch", "launch_sync"]
