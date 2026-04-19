"""Linear setup flow for ``agentbus setup``.

Organized as one top-level :func:`run_setup` that delegates to small
per-section helpers. The flow is:

1. Banner.
2. Existing-config detection (edit / overwrite / cancel).
3. Provider + model.
4. Built-in tools (bash, file_read, file_write, code_exec).
5. Memory (optional; enables ``memory_search`` tool).
6. Channels — a small loop that lets the user add zero or more
   channels. Each channel's sub-flow is owned by its plugin's
   :meth:`ChannelPlugin.interactive_setup`, which receives the same
   :class:`~agentbus.setup.prompter.Prompter` so the styling stays
   consistent end-to-end.
7. Atomic write + ``.bak`` rollback pointer.
8. ``agentbus doctor`` probe with the new config.
9. Outro.

All user-facing strings live here (not in the prompter) so tests that
use :class:`~agentbus.setup.prompter.FakePrompter` can assert on the
exact wording that drove each question.

The wizard never aborts mid-write: validation and channel sub-flows
run *before* the YAML is rendered, so a caught exception ends the
session without a half-written config. The backup file is replaced
atomically (tempfile + ``os.replace``) so even SIGKILL mid-rename
leaves either the old or new version visible — never a truncation.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from agentbus.channels import ChannelPlugin, ChannelRuntimeError, registered_plugins
from agentbus.channels.loader import _ensure_plugin_imported
from agentbus.chat._config import _PROVIDER_DEFAULTS, DEFAULT_CONFIG_PATH
from agentbus.setup.prompter import PromptCancelled, Prompter, QuestionaryPrompter

# ── shared constants ──────────────────────────────────────────────────────

_BUILTIN_CHANNELS = ("slack", "telegram")

_PROVIDER_CHOICES = [
    ("ollama", "ollama      — local inference via ollama"),
    ("mlx", "mlx         — Apple Silicon local inference"),
    ("anthropic", "anthropic   — Claude API (ANTHROPIC_API_KEY)"),
    ("openai", "openai      — GPT API (OPENAI_API_KEY)"),
]

_TOOL_CHOICES = [
    ("bash", "bash        — run shell commands"),
    ("file_read", "file_read   — read local files"),
    ("file_write", "file_write  — create or modify files"),
    ("code_exec", "code_exec   — run Python snippets"),
]


# ── entry point ───────────────────────────────────────────────────────────


def run_setup(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    *,
    prompter: Prompter | None = None,
    force: bool = False,
    run_doctor: bool = True,
) -> int:
    """Run the interactive setup flow. Returns a CLI-style exit code.

    * ``0`` — wrote config successfully (doctor warnings do not fail).
    * ``1`` — user cancelled (Ctrl-C, declined overwrite, chose cancel).
    * ``2`` — config validation / channel error before write.

    ``force=True`` skips the "existing config" prompt and overwrites.
    ``run_doctor=False`` skips the final probe (used by tests so a
    fresh config that references a network service doesn't flap).
    """
    path = Path(config_path)

    if prompter is None:
        try:
            prompter = QuestionaryPrompter()
        except ImportError:
            print(
                "error: `questionary` is not installed.\n  install with: uv sync --extra tui",
            )
            return 2

    try:
        from agentbus import __version__
    except Exception:
        __version__ = "0.0.0"

    try:
        prompter.banner(version=__version__)

        existing_raw = _load_existing(path)
        action = _decide_action(prompter, path, existing_raw, force=force)
        if action == "cancel":
            prompter.outro(str(path), wrote=False)
            return 1
        if action == "overwrite":
            existing_raw = {}

        provider, model = _ask_provider(prompter, existing_raw)
        tools = _ask_tools(prompter, existing_raw)
        memory = _ask_memory(prompter, existing_raw)
        channels = _ask_channels(prompter, existing_raw)

        new_data: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "tools": tools,
            "memory": memory,
        }
        if channels:
            new_data["channels"] = channels

        _atomic_write_yaml(path, new_data)
        prompter.note(f"wrote {path}", tone="success")

        if run_doctor:
            _run_doctor_section(prompter, path)

        prompter.outro(str(path), wrote=True)
        return 0

    except PromptCancelled:
        prompter.outro(str(path), wrote=False)
        return 1
    except ChannelRuntimeError as exc:
        prompter.note(f"channel setup failed: {exc}", tone="error")
        prompter.outro(str(path), wrote=False)
        return 2


# ── section: existing config ──────────────────────────────────────────────


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _decide_action(
    prompter: Prompter,
    path: Path,
    existing: dict[str, Any],
    *,
    force: bool,
) -> str:
    """Return ``"edit"``, ``"overwrite"``, or ``"cancel"``."""
    if not existing:
        return "overwrite"  # fresh start, nothing to merge
    if force:
        prompter.note(f"overwriting existing {path} (--force)", tone="warn")
        return "overwrite"
    choice = prompter.select(
        f"Found {path.name} — what would you like to do?",
        choices=[
            ("edit", "Edit — fill the form with existing values, update selectively"),
            ("overwrite", "Overwrite — start from a blank config"),
            ("cancel", "Cancel — leave the file alone"),
        ],
        default="edit",
    )
    return choice


# ── section: provider ─────────────────────────────────────────────────────


def _ask_provider(prompter: Prompter, existing: dict[str, Any]) -> tuple[str, str]:
    prompter.section(
        "Provider",
        "Which LLM backend should the chat planner use?",
    )
    current_provider = (
        existing.get("provider") if isinstance(existing.get("provider"), str) else None
    )
    provider = prompter.select(
        "Provider",
        choices=_PROVIDER_CHOICES,
        default=current_provider or "ollama",
    )
    default_model = existing.get("model") or _PROVIDER_DEFAULTS.get(provider, "")
    model = prompter.text(
        f"Model name for {provider}",
        default=str(default_model),
        validate=_require_non_empty,
    )
    return provider, model


# ── section: tools ────────────────────────────────────────────────────────


def _ask_tools(prompter: Prompter, existing: dict[str, Any]) -> list[str]:
    prompter.section(
        "Tools",
        "Built-in tools exposed to the planner (you can tighten these per-tool later).",
    )
    current = existing.get("tools")
    if not isinstance(current, list):
        current = ["bash", "file_read", "file_write"]
    return prompter.multi_select(
        "Enable tools",
        choices=_TOOL_CHOICES,
        default=current,
    )


# ── section: memory ───────────────────────────────────────────────────────


def _ask_memory(prompter: Prompter, existing: dict[str, Any]) -> bool:
    prompter.section(
        "Memory",
        "Embed each conversation turn into a local SQLite vector store and expose `memory_search`.",
    )
    current = existing.get("memory")
    if isinstance(current, dict):
        default = bool(current.get("enabled", False))
    else:
        default = bool(current) if current is not None else False
    enabled = prompter.confirm("Enable memory?", default=default)
    if enabled:
        prompter.note(
            "memory requires an embedding backend (default: ollama nomic-embed-text)",
            tone="muted",
        )
    return enabled


# ── section: channels ─────────────────────────────────────────────────────


def _ask_channels(prompter: Prompter, existing: dict[str, Any]) -> dict[str, dict]:
    prompter.section(
        "Channels",
        "Optional external gateways (Slack, Telegram). Leave empty to run terminal-only.",
    )
    existing_channels = existing.get("channels") or {}
    if not isinstance(existing_channels, dict):
        existing_channels = {}

    # Force-load builtin plugins so they show up in the select list.
    for name in _BUILTIN_CHANNELS:
        try:
            _ensure_plugin_imported(name)
        except ChannelRuntimeError as exc:
            prompter.note(f"{name}: unavailable ({exc})", tone="muted")

    available = registered_plugins()
    if not available:
        prompter.note("no channel plugins registered — skipping", tone="muted")
        return dict(existing_channels) if existing_channels else {}

    result: dict[str, dict] = dict(existing_channels)
    if not prompter.confirm(
        "Add or update a channel gateway?",
        default=bool(existing_channels),
    ):
        return result

    while True:
        remaining = sorted(available)
        if not remaining:
            break
        choices = [(name, _channel_label(name, result)) for name in remaining]
        choices.append(("__done__", "done — continue to write"))
        pick = prompter.select(
            "Which channel?",
            choices=choices,
            default="__done__",
        )
        if pick == "__done__":
            break
        plugin_cls = available[pick]
        prior = result.get(pick) if isinstance(result.get(pick), dict) else {}
        try:
            config_model = _invoke_channel_setup(plugin_cls, prompter, prior or {})
        except NotImplementedError:
            prompter.note(
                f"{pick!r} has no interactive setup — edit agentbus.yaml manually",
                tone="warn",
            )
            continue
        result[pick] = config_model.model_dump()
        prompter.note(f"configured channel: {pick}", tone="success")
        if not prompter.confirm("Configure another channel?", default=False):
            break
    return result


def _channel_label(name: str, existing: dict[str, Any]) -> str:
    status = "configured" if name in existing else "not set"
    return f"{name:10s}  ({status})"


def _invoke_channel_setup(
    plugin_cls: type[ChannelPlugin],
    prompter: Prompter,
    existing: dict[str, Any],
) -> Any:
    """Call the plugin's TUI setup hook.

    Defaults on the base class delegate to the legacy ``setup_wizard``;
    plugins that want the themed experience override ``interactive_setup``
    and drive the prompter directly. Either way, the caller only sees
    a validated :class:`pydantic.BaseModel`.
    """
    return plugin_cls.interactive_setup(prompter, existing)


# ── section: doctor ───────────────────────────────────────────────────────


def _run_doctor_section(prompter: Prompter, path: Path) -> None:
    """Run the doctor checks and render them through the prompter.

    We don't reuse ``agentbus.doctor.run`` directly — it prints to
    stdout with its own glyphs. Instead, we re-run the small check
    set and emit our themed notes so the wizard output is visually
    coherent.
    """
    prompter.section("Doctor", "Post-setup diagnostics.")
    from agentbus import doctor as _doctor

    checks = [
        _doctor._check_python(),
        _doctor._check_pydantic(),
        _doctor._check_sessions_dir(),
        _doctor._check_config(path),
        *_doctor._check_provider_deps(path),
        *_doctor._check_channels(path),
    ]
    for c in checks:
        tone = {"ok": "success", "warn": "warn", "fail": "error"}[c.status]
        label = c.name if not c.detail else f"{c.name} — {c.detail}"
        prompter.note(label, tone=tone)  # type: ignore[arg-type]


# ── helpers ───────────────────────────────────────────────────────────────


def _require_non_empty(value: str) -> str | None:
    if not value.strip():
        return "value required"
    return None


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` via tempfile + ``os.replace``.

    If ``path`` already exists, first copy it to ``path.with_suffix(path.suffix + ".bak")``
    so the user can recover if they change their mind. The backup copy
    happens *before* the new content is written, so even a crash during
    rename leaves ``path`` intact (either old or new).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)

    text = yaml.dump(data, default_flow_style=False, sort_keys=False)

    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
        tmp_name = fh.name
    os.replace(tmp_name, path)
