"""Tests for the chat sandbox layer.

Covers:
  * SubprocessSandbox: happy-path shell / python, timeout, output
    truncation, env scrubbing, workdir confinement.
  * DockerSandbox: constructor rejects missing binary; argv
    construction wires every security flag (no live docker required).
  * load_sandbox_from_dict: defaults, backend validation, errors.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from agentbus.chat._sandbox import (
    DEFAULT_CPU_SECONDS,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MEMORY_MB,
    DockerSandbox,
    SandboxConfig,
    SubprocessSandbox,
    build_sandbox,
    load_sandbox_from_dict,
)

# ── SubprocessSandbox ─────────────────────────────────────────────────────


async def test_subprocess_shell_happy_path() -> None:
    sb = SubprocessSandbox(SandboxConfig())
    result = await sb.run_shell("echo hello", timeout=5.0)
    assert result.exit_code == 0
    assert "hello" in result.output
    assert not result.timed_out
    assert not result.truncated


async def test_subprocess_python_happy_path() -> None:
    sb = SubprocessSandbox(SandboxConfig())
    result = await sb.run_python("print(2 + 2)", timeout=5.0)
    assert result.exit_code == 0
    assert "4" in result.output


async def test_subprocess_timeout_is_enforced() -> None:
    sb = SubprocessSandbox(SandboxConfig())
    result = await sb.run_shell("sleep 5", timeout=0.5)
    assert result.timed_out is True
    assert "terminated" in result.output


async def test_subprocess_output_truncation() -> None:
    sb = SubprocessSandbox(SandboxConfig(max_output_bytes=128))
    # Generate far more than 128 bytes of stdout.
    result = await sb.run_python(
        "print('x' * 10000)",
        timeout=5.0,
    )
    assert result.truncated is True
    assert "[output truncated" in result.output
    assert len(result.output) < 1024  # 128 + truncation note, not the full 10 KB


async def test_subprocess_workdir_is_temporary_by_default(tmp_path: Path) -> None:
    sb = SubprocessSandbox(SandboxConfig())
    result = await sb.run_shell("pwd", timeout=5.0)
    # Tempdir path leaks into output; assert the prefix we set.
    assert "agentbus-sandbox-" in result.output


async def test_subprocess_workdir_respects_config(tmp_path: Path) -> None:
    sb = SubprocessSandbox(SandboxConfig(workdir=str(tmp_path)))
    result = await sb.run_shell("pwd", timeout=5.0)
    # On macOS /tmp is a symlink to /private/tmp, so compare resolved paths.
    assert str(tmp_path.resolve()) in result.output or str(tmp_path) in result.output


async def test_subprocess_env_is_scrubbed_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A secret-shaped env var that must NOT leak into the sandbox.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    sb = SubprocessSandbox(SandboxConfig())
    result = await sb.run_shell("env", timeout=5.0)
    assert "ANTHROPIC_API_KEY" not in result.output
    assert "sk-ant-secret" not in result.output
    assert "PATH=" in result.output  # allowlist passes PATH through


async def test_subprocess_env_passthrough_is_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_testtoken")
    sb = SubprocessSandbox(SandboxConfig(env_passthrough=["GH_TOKEN"]))
    result = await sb.run_shell("env", timeout=5.0)
    assert "GH_TOKEN=ghp_testtoken" in result.output


async def test_subprocess_exit_code_nonzero() -> None:
    sb = SubprocessSandbox(SandboxConfig())
    result = await sb.run_shell("exit 7", timeout=5.0)
    assert result.exit_code == 7
    assert not result.timed_out


# ── DockerSandbox ─────────────────────────────────────────────────────────


def test_docker_sandbox_requires_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    # shutil.which returns None when docker is not on PATH.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="docker"):
        DockerSandbox(SandboxConfig(backend="docker"))


def test_docker_argv_sets_security_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    sb = DockerSandbox(
        SandboxConfig(
            backend="docker",
            cpu_seconds=15,
            memory_mb=256,
            image="python:3.12-slim",
            network=False,
        )
    )
    argv = sb._docker_argv("/tmp/host-workdir")
    assert argv[0:2] == ["docker", "run"]
    assert "--rm" in argv
    assert "--read-only" in argv
    assert "--memory" in argv and "256m" in argv
    assert "--cpus" in argv
    assert "--network" in argv and "none" in argv
    assert "-v" in argv
    assert any(a == "/tmp/host-workdir:/workspace:rw" for a in argv)
    assert "-w" in argv and "/workspace" in argv


def test_docker_argv_with_network_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    sb = DockerSandbox(SandboxConfig(backend="docker", network=True))
    argv = sb._docker_argv("/tmp/x")
    # --network none must NOT be present when network=true
    assert "none" not in argv


def test_docker_argv_passes_env_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setenv("GH_TOKEN", "ghp_xyz")
    sb = DockerSandbox(SandboxConfig(backend="docker", env_passthrough=["GH_TOKEN"]))
    argv = sb._docker_argv("/tmp/x")
    assert "-e" in argv
    assert any(a == "GH_TOKEN=ghp_xyz" for a in argv)


# ── config loader ─────────────────────────────────────────────────────────


def test_load_sandbox_empty_returns_defaults() -> None:
    cfg = load_sandbox_from_dict(None)
    assert cfg.backend == "subprocess"
    assert cfg.cpu_seconds == DEFAULT_CPU_SECONDS
    assert cfg.memory_mb == DEFAULT_MEMORY_MB
    assert cfg.max_output_bytes == DEFAULT_MAX_OUTPUT_BYTES


def test_load_sandbox_parses_full_block() -> None:
    cfg = load_sandbox_from_dict(
        {
            "backend": "docker",
            "cpu_seconds": 10,
            "memory_mb": 128,
            "max_output_bytes": 1024,
            "image": "alpine:latest",
            "network": True,
            "env_passthrough": ["GH_TOKEN", "OPENAI_API_KEY"],
        }
    )
    assert cfg.backend == "docker"
    assert cfg.cpu_seconds == 10
    assert cfg.memory_mb == 128
    assert cfg.max_output_bytes == 1024
    assert cfg.image == "alpine:latest"
    assert cfg.network is True
    assert cfg.env_passthrough == ["GH_TOKEN", "OPENAI_API_KEY"]


def test_load_sandbox_rejects_bad_backend() -> None:
    with pytest.raises(ValueError, match="backend"):
        load_sandbox_from_dict({"backend": "chroot"})


def test_load_sandbox_rejects_non_dict() -> None:
    with pytest.raises(ValueError, match="mapping"):
        load_sandbox_from_dict("oops")  # type: ignore[arg-type]


def test_load_sandbox_rejects_non_list_passthrough() -> None:
    with pytest.raises(ValueError, match="env_passthrough"):
        load_sandbox_from_dict({"env_passthrough": "GH_TOKEN"})


def test_build_sandbox_selects_backend() -> None:
    sb = build_sandbox(SandboxConfig(backend="subprocess"))
    assert isinstance(sb, SubprocessSandbox)


def test_build_sandbox_docker_requires_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        build_sandbox(SandboxConfig(backend="docker"))


# Skip the env-scrub assertion on Windows just in case someone runs the
# suite there — `env` isn't a shell builtin everywhere.
if sys.platform == "win32":  # pragma: no cover
    del test_subprocess_env_is_scrubbed_by_default
    del test_subprocess_env_passthrough_is_honored
