"""Tests for the interactive setup wizard.

These tests drive the wizard with a :class:`FakePrompter` that replays
a scripted answer list. That way the flow is exercised end-to-end —
every section, every prompt — without a TTY.

What the fake prompter does NOT check: ANSI rendering, questionary
keybinds, terminal cursor handling. Those belong in a manual-smoke
test (``uv run agentbus setup`` in a real terminal), not pytest.

The tests use ``tmp_path`` directly since there's no AF_UNIX socket
involved — the path length limit that applies to ``tests/test_bus.py``
doesn't bite here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agentbus.setup import FakePrompter, PromptCancelled, run_setup
from agentbus.setup import wizard as wiz

# ── helpers ───────────────────────────────────────────────────────────────


def _fresh_run(
    answers: list[Any],
    *,
    tmp_path: Path,
    config_name: str = "agentbus.yaml",
    force: bool = False,
    run_doctor: bool = False,
) -> tuple[int, FakePrompter, Path]:
    prompter = FakePrompter(answers)
    path = tmp_path / config_name
    code = run_setup(path, prompter=prompter, force=force, run_doctor=run_doctor)
    return code, prompter, path


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ── fresh install ─────────────────────────────────────────────────────────


class TestFreshInstall:
    def test_minimal_flow_writes_config(self, tmp_path: Path) -> None:
        """Smallest happy-path run: ollama, default model, no tools, no memory, no channels."""
        answers = [
            "ollama",  # provider
            "llama3.1:8b-instruct",  # model
            [],  # tools multi_select
            False,  # memory
            False,  # add channels?
        ]
        code, _, path = _fresh_run(answers, tmp_path=tmp_path)
        assert code == 0
        data = _load_yaml(path)
        assert data["provider"] == "ollama"
        assert data["model"] == "llama3.1:8b-instruct"
        assert data["tools"] == []
        assert data["memory"] is False
        assert "channels" not in data

    def test_default_tools_retained_when_list_scripted(self, tmp_path: Path) -> None:
        answers = [
            "anthropic",
            "claude-haiku-4-5-20251001",
            ["bash", "file_read", "file_write"],
            False,
            False,
        ]
        code, _, path = _fresh_run(answers, tmp_path=tmp_path)
        assert code == 0
        data = _load_yaml(path)
        assert data["tools"] == ["bash", "file_read", "file_write"]

    def test_require_non_empty_validator(self) -> None:
        assert wiz._require_non_empty("ollama") is None
        assert wiz._require_non_empty("  ") == "value required"

    def test_memory_enabled_flag_persists(self, tmp_path: Path) -> None:
        answers = ["ollama", "llama3.1", [], True, False]
        code, _, path = _fresh_run(answers, tmp_path=tmp_path)
        assert code == 0
        assert _load_yaml(path)["memory"] is True


# ── cancellation paths ────────────────────────────────────────────────────


class TestCancellation:
    def test_script_exhaustion_is_cancel(self, tmp_path: Path) -> None:
        """FakePrompter raises PromptCancelled when the script runs dry;
        the wizard catches it and reports exit code 1 without writing."""
        code, prompter, path = _fresh_run(
            ["ollama"],  # too few answers; will cancel at model prompt
            tmp_path=tmp_path,
        )
        assert code == 1
        assert not path.exists()
        # Outro was called with wrote=False
        assert any(kind == "outro" and "cancelled" in detail for kind, detail in prompter.output)

    def test_user_cancels_at_existing_config_prompt(self, tmp_path: Path) -> None:
        path = tmp_path / "agentbus.yaml"
        path.write_text("provider: openai\nmodel: gpt-4o\n", encoding="utf-8")
        prompter = FakePrompter(["cancel"])
        code = run_setup(path, prompter=prompter, run_doctor=False)
        assert code == 1
        # Didn't overwrite
        assert _load_yaml(path)["provider"] == "openai"


# ── existing config: edit / overwrite ─────────────────────────────────────


class TestExistingConfig:
    def _write_existing(self, path: Path, **overrides: Any) -> None:
        base: dict[str, Any] = {
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "tools": ["bash"],
            "memory": True,
        }
        base.update(overrides)
        path.write_text(yaml.dump(base), encoding="utf-8")

    def test_edit_path_keeps_defaults_when_blank_text(self, tmp_path: Path) -> None:
        path = tmp_path / "agentbus.yaml"
        self._write_existing(path)
        # edit → keep provider, reuse model (blank uses default=existing), keep tools, keep memory, no channels
        answers = [
            "edit",  # action
            "anthropic",
            "",  # blank model → default=existing → "claude-haiku-4-5-20251001"
            ["bash"],
            True,
            False,
        ]
        prompter = FakePrompter(answers)
        code = run_setup(path, prompter=prompter, run_doctor=False)
        assert code == 0
        data = _load_yaml(path)
        assert data["model"] == "claude-haiku-4-5-20251001"
        assert data["memory"] is True

    def test_overwrite_clears_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "agentbus.yaml"
        self._write_existing(path, tools=["bash", "file_read", "file_write"])
        answers = [
            "overwrite",
            "ollama",
            "llama3.1",
            [],
            False,
            False,
        ]
        prompter = FakePrompter(answers)
        code = run_setup(path, prompter=prompter, run_doctor=False)
        assert code == 0
        data = _load_yaml(path)
        assert data["provider"] == "ollama"
        assert data["tools"] == []

    def test_force_skips_action_prompt(self, tmp_path: Path) -> None:
        path = tmp_path / "agentbus.yaml"
        self._write_existing(path)
        # no 'action' answer — --force skips it
        answers = ["ollama", "llama3.1", [], False, False]
        prompter = FakePrompter(answers)
        code = run_setup(path, prompter=prompter, force=True, run_doctor=False)
        assert code == 0

    def test_backup_file_written_on_overwrite(self, tmp_path: Path) -> None:
        path = tmp_path / "agentbus.yaml"
        original = "provider: openai\nmodel: gpt-4o\n"
        path.write_text(original, encoding="utf-8")
        answers = ["overwrite", "ollama", "llama3.1", [], False, False]
        prompter = FakePrompter(answers)
        code = run_setup(path, prompter=prompter, run_doctor=False)
        assert code == 0
        backup = tmp_path / "agentbus.yaml.bak"
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == original


# ── channels sub-flow ─────────────────────────────────────────────────────


class TestChannels:
    def test_skip_channels_entirely(self, tmp_path: Path) -> None:
        answers = ["ollama", "llama3.1", [], False, False]
        code, _, path = _fresh_run(answers, tmp_path=tmp_path)
        assert code == 0
        assert "channels" not in _load_yaml(path)

    def test_configure_telegram(self, tmp_path: Path) -> None:
        # Force-load the plugin — same dance as the CLI does.
        from agentbus.channels.loader import _ensure_plugin_imported

        _ensure_plugin_imported("telegram")

        answers = [
            "ollama",
            "llama3.1",
            [],
            False,
            True,  # add a channel
            "telegram",  # which channel
            "xxxxxxxxxx:yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",  # bot_token
            "",  # allowed_chats blank
            False,  # add another?
        ]
        code, _, path = _fresh_run(answers, tmp_path=tmp_path)
        assert code == 0
        data = _load_yaml(path)
        assert "telegram" in data["channels"]
        assert data["channels"]["telegram"]["bot_token"].startswith("xxxxxxxxxx")

    def test_configure_slack_then_done(self, tmp_path: Path) -> None:
        from agentbus.channels.loader import _ensure_plugin_imported

        _ensure_plugin_imported("slack")

        answers = [
            "ollama",
            "llama3.1",
            [],
            False,
            True,  # add channel
            "slack",
            "xapp-token",  # app_token
            "xoxb-token",  # bot_token
            "",  # allowed channels
            "",  # allowed senders
            True,  # ignore_bots
            False,  # done adding
        ]
        code, _, path = _fresh_run(answers, tmp_path=tmp_path)
        assert code == 0
        data = _load_yaml(path)
        assert data["channels"]["slack"]["app_token"] == "xapp-token"
        assert data["channels"]["slack"]["ignore_bots"] is True

    def test_telegram_invalid_allowed_chat_rejected(self, tmp_path: Path) -> None:
        from agentbus.channels.loader import _ensure_plugin_imported

        _ensure_plugin_imported("telegram")

        answers = [
            "ollama",
            "llama3.1",
            [],
            False,
            True,
            "telegram",
            "token",
            "12345,not_an_int",  # validator should reject
            False,
        ]
        with pytest.raises(AssertionError, match="not a valid integer"):
            _fresh_run(answers, tmp_path=tmp_path)


# ── atomic write / backup ──────────────────────────────────────────────────


class TestAtomicWrite:
    def test_atomic_write_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "new" / "subdir" / "agentbus.yaml"
        wiz._atomic_write_yaml(nested, {"provider": "ollama"})
        assert nested.exists()

    def test_atomic_write_leaves_no_tmp_file(self, tmp_path: Path) -> None:
        path = tmp_path / "agentbus.yaml"
        wiz._atomic_write_yaml(path, {"provider": "ollama"})
        siblings = sorted(p.name for p in tmp_path.iterdir())
        assert siblings == ["agentbus.yaml"]

    def test_atomic_write_backs_up_prior_content(self, tmp_path: Path) -> None:
        path = tmp_path / "agentbus.yaml"
        path.write_text("old: true\n", encoding="utf-8")
        wiz._atomic_write_yaml(path, {"new": True})
        assert (tmp_path / "agentbus.yaml.bak").read_text(encoding="utf-8") == "old: true\n"
        assert _load_yaml(path) == {"new": True}


# ── theme smoke tests ─────────────────────────────────────────────────────


class TestTheme:
    def test_banner_renders_without_color_when_requested(self, monkeypatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        from agentbus.setup import theme

        banner = theme.render_banner("1.2.3")
        assert "\033[" not in banner
        assert "1.2.3" in banner

    def test_section_includes_subtitle(self) -> None:
        from agentbus.setup import theme

        rendered = theme.render_section("Provider", "which LLM?")
        assert "Provider" in rendered
        assert "which LLM?" in rendered

    def test_note_maps_tones(self) -> None:
        from agentbus.setup import theme

        for tone, glyph in (("success", "✓"), ("warn", "!"), ("error", "✗")):
            assert glyph in theme.render_note("hi", tone=tone)


# ── FakePrompter self-checks ──────────────────────────────────────────────


class TestFakePrompter:
    def test_runs_validator_on_text(self) -> None:
        p = FakePrompter([""])
        with pytest.raises(AssertionError):
            p.text("msg", validate=lambda v: None if v else "required")

    def test_script_exhaustion_raises_cancelled(self) -> None:
        p = FakePrompter([])
        with pytest.raises(PromptCancelled):
            p.text("msg")

    def test_select_rejects_out_of_range(self) -> None:
        p = FakePrompter(["zebra"])
        with pytest.raises(AssertionError, match="valid choices"):
            p.select("pick", choices=[("a", "A"), ("b", "B")])

    def test_multi_select_type_check(self) -> None:
        p = FakePrompter(["not a list"])
        with pytest.raises(AssertionError, match="expected list"):
            p.multi_select("pick", choices=[("a", "A")])
