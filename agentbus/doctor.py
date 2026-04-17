"""`agentbus doctor` — diagnostic checks for a local install.

Exits non-zero on any failure so it's CI-friendly and suitable as a
post-install smoke test.

The checks are deliberately cheap and side-effect-free: no network calls,
no subprocess spawns, and no mutations outside `~/.agentbus/sessions/`
(which is touched only by an atomic write/delete probe).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_OK = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"
_WARN = "\033[33m!\033[0m"


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""

    def render(self) -> str:
        glyph = {"ok": _OK, "warn": _WARN, "fail": _FAIL}[self.status]
        line = f"  {glyph} {self.name}"
        if self.detail:
            line += f"  — {self.detail}"
        return line


# ── individual checks ─────────────────────────────────────────────────────


def _check_python() -> Check:
    if sys.version_info < (3, 12):  # noqa: UP036 — runtime diagnostic, not a guard
        return Check(
            "Python >= 3.12",
            "fail",
            f"running {sys.version_info.major}.{sys.version_info.minor}",
        )
    return Check("Python >= 3.12", "ok", f"{sys.version_info.major}.{sys.version_info.minor}")


def _check_pydantic() -> Check:
    try:
        import pydantic

        v = pydantic.VERSION
        if not v.startswith("2."):
            return Check("pydantic >= 2.0", "fail", f"found {v}")
        return Check("pydantic >= 2.0", "ok", v)
    except ModuleNotFoundError:
        return Check("pydantic >= 2.0", "fail", "not installed")


def _check_sessions_dir() -> Check:
    root = Path.home() / ".agentbus" / "sessions"
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Atomic write probe.
        with tempfile.NamedTemporaryFile(dir=root, delete=True) as fh:
            fh.write(b"probe")
        return Check("~/.agentbus/sessions writable", "ok", str(root))
    except Exception as exc:
        return Check("~/.agentbus/sessions writable", "fail", str(exc))


def _check_config(path: Path) -> Check:
    from agentbus.chat._config import ChatConfig, load_config

    if not path.exists():
        return Check(f"{path} present", "warn", "not found (chat will launch first-run wizard)")
    try:
        cfg = load_config(path)
        assert isinstance(cfg, ChatConfig)
        return Check(
            f"{path} valid",
            "ok",
            f"provider={cfg.provider} model={cfg.model} tools={len(cfg.tools)}",
        )
    except Exception as exc:
        return Check(f"{path} valid", "fail", str(exc))


def _check_provider_deps(path: Path) -> list[Check]:
    """Check the dep for whichever provider is in the config (best-effort)."""
    if not path.exists():
        return [Check("provider dependency", "warn", "no config — skipped")]
    try:
        from agentbus.chat._config import load_config

        cfg = load_config(path)
    except Exception:
        return [Check("provider dependency", "warn", "config failed to parse")]

    provider_pkg = {
        "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
        "openai": ("openai", "OPENAI_API_KEY"),
        "ollama": ("httpx", None),
        "mlx": ("httpx", None),
    }.get(cfg.provider)

    if provider_pkg is None:
        return [Check(f"provider {cfg.provider!r}", "fail", "unknown provider")]

    pkg, env = provider_pkg
    checks: list[Check] = []
    try:
        importlib.import_module(pkg)
        checks.append(Check(f"{pkg} importable", "ok", ""))
    except ModuleNotFoundError:
        checks.append(
            Check(
                f"{pkg} importable",
                "fail",
                f"install with: uv sync --extra {cfg.provider}",
            )
        )

    if env is not None:
        if os.environ.get(env):
            checks.append(Check(f"${env} set", "ok", ""))
        else:
            checks.append(Check(f"${env} set", "warn", "not set — API calls will fail"))
    return checks


def _check_socket(path: str) -> Check:
    """Only reports OK if the socket exists AND responds to a `topics` command."""
    if not Path(path).exists():
        return Check(f"bus socket at {path}", "warn", "no bus running (expected offline)")

    async def _probe() -> tuple[bool, str]:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), timeout=2.0)
        except (ConnectionRefusedError, FileNotFoundError, TimeoutError, OSError) as exc:
            return False, f"cannot connect: {exc}"
        try:
            writer.write(b'{"cmd":"topics"}\n')
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            _ = json.loads(line)
            return True, "responsive"
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    try:
        ok, detail = asyncio.run(_probe())
    except Exception as exc:
        return Check(f"bus socket at {path}", "fail", str(exc))

    return Check(f"bus socket at {path}", "ok" if ok else "fail", detail)


def _check_socket_path_length(path: str) -> Check:
    """macOS UNIX sockets are limited to 104 bytes (sun_path)."""
    on_darwin = sys.platform == "darwin"
    if on_darwin and len(path) > 103:
        return Check(
            "socket path length",
            "fail",
            f"{len(path)} bytes > 103 — macOS AF_UNIX limit",
        )
    suffix = "" if on_darwin else " (non-macOS)"
    return Check("socket path length", "ok", f"{len(path)} bytes{suffix}")


# ── entry point ───────────────────────────────────────────────────────────


def run(config_path: Path | str = "agentbus.yaml", socket_path: str | None = None) -> int:
    """Run all diagnostics and return the exit code (0 = all green)."""
    from agentbus import __version__

    socket_path = socket_path or "/tmp/agentbus.sock"
    cfg = Path(config_path)

    print(f"agentbus doctor — v{__version__}")
    print()

    checks: list[Check] = [
        _check_python(),
        _check_pydantic(),
        _check_sessions_dir(),
        _check_socket_path_length(socket_path),
        _check_config(cfg),
        *_check_provider_deps(cfg),
        _check_socket(socket_path),
    ]

    for c in checks:
        print(c.render())

    # Exit non-zero only on hard failures; warnings are informational.
    exit_code = 1 if any(c.status == "fail" for c in checks) else 0
    print()
    if exit_code == 0:
        failed = sum(1 for c in checks if c.status == "warn")
        print(f"All checks passed ({failed} warnings).")
    else:
        failed = sum(1 for c in checks if c.status == "fail")
        print(f"{failed} check(s) failed.")
    return exit_code
