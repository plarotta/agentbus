import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from agentbus.launch import launch_sync
from agentbus.logging_config import setup_logging

DEFAULT_SOCKET_PATH = "/tmp/agentbus.sock"


async def _socket_request(
    payload: dict,
    *,
    socket_path: str = DEFAULT_SOCKET_PATH,
    stream: bool = False,
) -> list[dict] | dict:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        if stream:
            responses = []
            while True:
                line = await reader.readline()
                if not line:
                    break
                responses.append(json.loads(line))
            return responses
        line = await reader.readline()
        if not line:
            return {}
        return json.loads(line)
    finally:
        writer.close()
        await writer.wait_closed()


def _format_json(data) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _format_mermaid(graph: dict) -> str:
    lines = ["graph TD"]
    for edge in graph.get("edges", []):
        node = edge["node"]
        topic = edge["topic"]
        if edge["direction"] == "pub":
            lines.append(f'  "{node}" --> "{topic}"')
        else:
            lines.append(f'  "{topic}" --> "{node}"')
    return "\n".join(lines)


def _format_dot(graph: dict) -> str:
    lines = ["digraph AgentBus {"]
    for edge in graph.get("edges", []):
        node = edge["node"]
        topic = edge["topic"]
        if edge["direction"] == "pub":
            lines.append(f'  "{node}" -> "{topic}";')
        else:
            lines.append(f'  "{topic}" -> "{node}";')
    lines.append("}")
    return "\n".join(lines)


def _join_lines(items: Iterable[str]) -> str:
    return "\n".join(items)


def topic_list(*, socket_path: str = DEFAULT_SOCKET_PATH) -> str:
    data = asyncio.run(_socket_request({"cmd": "topics"}, socket_path=socket_path))
    return _format_json(data)


def topic_echo(topic: str, *, n: int | None = None, socket_path: str = DEFAULT_SOCKET_PATH) -> str:
    payload = {"cmd": "echo", "topic": topic}
    if n is not None:
        payload["n"] = n
    data = asyncio.run(_socket_request(payload, socket_path=socket_path, stream=True))
    return _join_lines(json.dumps(item, sort_keys=True) for item in data)


def node_list(*, socket_path: str = DEFAULT_SOCKET_PATH) -> str:
    data = asyncio.run(_socket_request({"cmd": "nodes"}, socket_path=socket_path))
    return _format_json(data)


def node_info(name: str, *, socket_path: str = DEFAULT_SOCKET_PATH) -> str:
    data = asyncio.run(_socket_request({"cmd": "node_info", "name": name}, socket_path=socket_path))
    return _format_json(data)


def graph(*, format: str = "json", socket_path: str = DEFAULT_SOCKET_PATH) -> str:
    data = asyncio.run(_socket_request({"cmd": "graph"}, socket_path=socket_path))
    if format == "json":
        return _format_json(data)
    if format == "mermaid":
        return _format_mermaid(data)
    if format == "dot":
        return _format_dot(data)
    raise ValueError(f"Unsupported graph format: {format}")


def build_parser() -> argparse.ArgumentParser:
    from agentbus import __version__

    parser = argparse.ArgumentParser(prog="agentbus")
    parser.add_argument("--version", action="version", version=f"agentbus {__version__}")
    parser.add_argument("--socket-path", default=DEFAULT_SOCKET_PATH)
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level: DEBUG | INFO | WARNING | ERROR (overrides AGENTBUS_LOG_LEVEL)",
    )
    parser.add_argument(
        "--log-format",
        default=None,
        choices=["text", "json"],
        help="Log format (overrides AGENTBUS_LOG_FORMAT)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── chat ──────────────────────────────────────────────────────────────────
    chat_parser = subparsers.add_parser(
        "chat",
        help="Launch interactive chat mode",
    )
    chat_parser.add_argument(
        "--config",
        default="agentbus.yaml",
        metavar="PATH",
        help="Path to agentbus.yaml (default: ./agentbus.yaml)",
    )
    chat_parser.add_argument(
        "--provider",
        default=None,
        help="Override provider from config (ollama, mlx, anthropic, openai)",
    )
    chat_parser.add_argument(
        "--model",
        default=None,
        help="Override model from config",
    )
    chat_parser.add_argument(
        "--session",
        default=None,
        metavar="ID",
        help="Resume a previous session by ID",
    )
    chat_parser.add_argument(
        "--no-memory",
        action="store_true",
        default=False,
        help="Disable MemoryNode even if configured",
    )
    chat_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Show tool dispatches inline",
    )
    chat_parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress tool dispatches (overrides --verbose)",
    )
    chat_parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="No TUI — plain stdin/stdout, for piping",
    )

    # ── topic ─────────────────────────────────────────────────────────────────
    topic_parser = subparsers.add_parser("topic")
    topic_subparsers = topic_parser.add_subparsers(dest="topic_command", required=True)
    topic_subparsers.add_parser("list")
    topic_echo_parser = topic_subparsers.add_parser("echo")
    topic_echo_parser.add_argument("topic")
    topic_echo_parser.add_argument("--n", type=int, default=None)

    # ── node ──────────────────────────────────────────────────────────────────
    node_parser = subparsers.add_parser("node")
    node_subparsers = node_parser.add_subparsers(dest="node_command", required=True)
    node_subparsers.add_parser("list")
    node_info_parser = node_subparsers.add_parser("info")
    node_info_parser.add_argument("name")

    # ── graph ─────────────────────────────────────────────────────────────────
    graph_parser = subparsers.add_parser("graph")
    graph_parser.add_argument("--format", default="json", choices=["json", "mermaid", "dot"])

    # ── launch ────────────────────────────────────────────────────────────────
    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("config_path")

    # ── doctor ────────────────────────────────────────────────────────────────
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run local-install diagnostics (python, deps, config, socket)",
    )
    doctor_parser.add_argument("--config", default="agentbus.yaml", help="Path to agentbus.yaml")

    # ── setup ────────────────────────────────────────────────────────────────
    setup_parser = subparsers.add_parser(
        "setup",
        help="Interactive setup wizard — provider, tools, memory, channels",
    )
    setup_parser.add_argument(
        "--config",
        default="agentbus.yaml",
        help="Path to agentbus.yaml to create or update (default: ./agentbus.yaml)",
    )
    setup_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite existing config without prompting",
    )
    setup_parser.add_argument(
        "--skip-doctor",
        action="store_true",
        default=False,
        help="Skip the post-setup doctor probe",
    )

    # ── channels ─────────────────────────────────────────────────────────────
    channels_parser = subparsers.add_parser(
        "channels",
        help="Manage multi-channel gateways (Slack, Telegram, …)",
    )
    channels_sub = channels_parser.add_subparsers(dest="channels_command", required=True)

    channels_list = channels_sub.add_parser("list", help="List registered channel plugins")
    channels_list.add_argument("--config", default="agentbus.yaml")

    channels_setup = channels_sub.add_parser(
        "setup",
        help="Interactive setup for a channel plugin (writes to agentbus.yaml)",
    )
    channels_setup.add_argument("channel", help="Channel name (e.g. slack, telegram)")
    channels_setup.add_argument("--config", default="agentbus.yaml")

    # ── daemon ───────────────────────────────────────────────────────────────
    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Run agentbus as a long-lived foreground daemon",
    )
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command", required=True)

    daemon_start = daemon_sub.add_parser("start", help="Start the daemon (foreground)")
    daemon_start.add_argument("config_path")
    daemon_start.add_argument("--pidfile", default=None, help="Override the default pidfile path")

    daemon_stop = daemon_sub.add_parser("stop", help="Send SIGTERM and wait for exit")
    daemon_stop.add_argument("--pidfile", default=None)
    daemon_stop.add_argument(
        "--timeout", type=float, default=10.0, help="Seconds to wait for graceful exit"
    )

    daemon_status = daemon_sub.add_parser("status", help="Report running state")
    daemon_status.add_argument("--pidfile", default=None)

    daemon_tpl = daemon_sub.add_parser(
        "install",
        help="Print a systemd unit or launchd plist for the supplied config",
    )
    daemon_tpl.add_argument("kind", choices=["systemd", "launchd"])
    daemon_tpl.add_argument("config_path")
    daemon_tpl.add_argument(
        "--label", default="com.agentbus.daemon", help="launchd Label (launchd only)"
    )

    return parser


def _run_chat(args) -> int:
    """Entry point for `agentbus chat`."""
    from agentbus.chat._config import first_run_wizard, load_config

    config_path = Path(args.config)

    # First-run: no config file found
    if not config_path.exists():
        try:
            config = first_run_wizard(config_path)
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.", file=sys.stderr)
            return 1
    else:
        config = load_config(config_path)

    # CLI overrides
    if args.provider:
        config.provider = args.provider
    if args.model:
        config.model = args.model
    if args.no_memory:
        config.memory = False
        config.memory_settings = {"enabled": False}

    # verbose precedence: --quiet > --verbose > auto
    verbose: bool | None = None
    if args.quiet:
        verbose = False
    elif args.verbose:
        verbose = True

    from agentbus.chat._runner import run_chat

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            run_chat(
                config,
                headless=args.headless,
                verbose=verbose,
                session_id=args.session,
            )
        )
    return 0


def app(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(level=args.log_level, format=args.log_format)

    if args.command == "chat":
        return _run_chat(args)
    if args.command == "topic" and args.topic_command == "list":
        print(topic_list(socket_path=args.socket_path))
        return 0
    if args.command == "topic" and args.topic_command == "echo":
        print(topic_echo(args.topic, n=args.n, socket_path=args.socket_path))
        return 0
    if args.command == "node" and args.node_command == "list":
        print(node_list(socket_path=args.socket_path))
        return 0
    if args.command == "node" and args.node_command == "info":
        print(node_info(args.name, socket_path=args.socket_path))
        return 0
    if args.command == "graph":
        print(graph(format=args.format, socket_path=args.socket_path))
        return 0
    if args.command == "launch":
        launch_sync(args.config_path)
        return 0
    if args.command == "doctor":
        from agentbus.doctor import run as _run_doctor

        return _run_doctor(config_path=args.config, socket_path=args.socket_path)
    if args.command == "setup":
        from agentbus.setup import run_setup

        return run_setup(
            config_path=args.config,
            force=args.force,
            run_doctor=not args.skip_doctor,
        )
    if args.command == "daemon":
        return _run_daemon(args)
    if args.command == "channels":
        return _run_channels(args)

    parser.error("unknown command")
    return 2


def _run_channels(args) -> int:
    from agentbus.channels import (
        ChannelRuntimeError,
        registered_plugins,
    )
    from agentbus.channels.loader import _ensure_plugin_imported

    config_path = Path(args.config)

    if args.channels_command == "list":
        # Force-load builtin plugins so their names show up.
        for name in ("slack", "telegram"):
            try:
                _ensure_plugin_imported(name)
            except ChannelRuntimeError as exc:
                print(f"  {name}: unavailable ({exc})", file=sys.stderr)
        for name, plugin_cls in sorted(registered_plugins().items()):
            print(f"  {name:10s}  {plugin_cls.__module__}.{plugin_cls.__name__}")
        return 0

    if args.channels_command == "setup":
        try:
            _ensure_plugin_imported(args.channel)
        except ChannelRuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        plugin_cls = registered_plugins().get(args.channel)
        if plugin_cls is None:
            print(f"error: unknown channel plugin {args.channel!r}", file=sys.stderr)
            return 1

        import yaml

        existing_full: dict = {}
        if config_path.exists():
            existing_full = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        existing_channel = (existing_full.get("channels") or {}).get(args.channel) or {}
        try:
            config = plugin_cls.setup_wizard(dict(existing_channel))
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.", file=sys.stderr)
            return 1
        except NotImplementedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        existing_full.setdefault("channels", {})[args.channel] = config.model_dump()
        config_path.write_text(
            yaml.dump(existing_full, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        print(f"Wrote channels.{args.channel} to {config_path}")
        return 0

    print(f"unknown channels subcommand: {args.channels_command}", file=sys.stderr)
    return 2


def _run_daemon(args) -> int:
    from agentbus import daemon

    pidfile = Path(args.pidfile) if getattr(args, "pidfile", None) else daemon.DEFAULT_PID_PATH

    if args.daemon_command == "start":
        return daemon.run(args.config_path, pidfile=pidfile)
    if args.daemon_command == "stop":
        ok = daemon.stop(pidfile, timeout=args.timeout)
        if ok:
            print(f"daemon stopped (pidfile={pidfile})")
            return 0
        st = daemon.status(pidfile)
        print(st.describe(), file=sys.stderr)
        return 1
    if args.daemon_command == "status":
        st = daemon.status(pidfile)
        print(st.describe())
        return 0 if st.running else 1
    if args.daemon_command == "install":
        if args.kind == "systemd":
            print(daemon.emit_systemd_unit(args.config_path))
        else:
            print(daemon.emit_launchd_plist(args.config_path, label=args.label))
        return 0

    print(f"unknown daemon subcommand: {args.daemon_command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(app())
