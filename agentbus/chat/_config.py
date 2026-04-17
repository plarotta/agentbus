"""Chat mode configuration — ChatConfig model, YAML loading, first-run wizard."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("agentbus.yaml")
DEFAULT_SESSIONS_ROOT = Path.home() / ".agentbus" / "sessions"

_PROVIDER_DEFAULTS: dict[str, str] = {
    "ollama": "llama3.1:8b-instruct",
    "mlx": "mlx-community/Llama-3.2-3B-Instruct-4bit",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}


@dataclass
class ChatConfig:
    provider: str = "ollama"
    model: str = "llama3.1:8b-instruct"
    tools: list[str] = field(default_factory=lambda: ["bash", "file_read", "file_write"])
    memory: bool = False

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        data = {
            "provider": self.provider,
            "model": self.model,
            "tools": self.tools,
            "memory": self.memory,
        }
        try:
            import yaml  # type: ignore[import-not-found]

            path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        except ModuleNotFoundError:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ChatConfig:
    """Load ChatConfig from a YAML or JSON file."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]

        data = yaml.safe_load(text)
    except ModuleNotFoundError:
        data = json.loads(text)

    return ChatConfig(
        provider=data.get("provider", "ollama"),
        model=data.get(
            "model", _PROVIDER_DEFAULTS.get(data.get("provider", "ollama"), "llama3.1:8b-instruct")
        ),
        tools=data.get("tools", ["bash", "file_read", "file_write"]),
        memory=data.get("memory", False),
    )


def first_run_wizard(path: Path = DEFAULT_CONFIG_PATH) -> ChatConfig:
    """Interactive first-run setup when no agentbus.yaml exists.

    Uses plain input() prompts — no external dependencies required.
    """
    print("Welcome to AgentBus.\n\nNo config found. Let's set up quickly.\n")

    providers = list(_PROVIDER_DEFAULTS)
    labels = [
        "ollama (local — requires ollama running)",
        "mlx (local — Apple Silicon only)",
        "anthropic (API key required)",
        "openai (API key required)",
    ]

    print("? Provider")
    for i, label in enumerate(labels):
        marker = "❯" if i == 0 else " "
        print(f"  {marker} {label}")

    raw_provider = input("\nProvider [ollama]: ").strip().lower()
    provider = raw_provider if raw_provider in providers else "ollama"

    default_model = _PROVIDER_DEFAULTS[provider]
    raw_model = input(f"Model [{default_model}]: ").strip()
    model = raw_model or default_model

    raw_tools = input("Enable default tools (bash, file_read, file_write)? [Y/n]: ").strip().lower()
    tools = ["bash", "file_read", "file_write"] if raw_tools not in ("n", "no") else []

    raw_memory = input("Enable memory (RAG + vector store)? [y/N]: ").strip().lower()
    memory = raw_memory in ("y", "yes")

    config = ChatConfig(provider=provider, model=model, tools=tools, memory=memory)
    config.save(path)

    tool_desc = ", ".join(tools) if tools else "none"
    print(f"\n✓ Wrote {path}")
    print(
        f"✓ Starting bus with {len(tools) + 1} nodes (planner{', ' + tool_desc if tools else ''})\n"
    )
    return config
