"""Sandbox layer for `bash` and `code_exec` tools.

Two backends:

* :class:`SubprocessSandbox` — the default. Runs the command as a child
  process under `resource.setrlimit` CPU and address-space caps applied
  via `preexec_fn`, with a scrubbed environment (allowlist only) and a
  configurable working directory. Output is truncated to
  ``max_output_bytes`` before being returned so a runaway process can't
  blow the LLM's context window.
* :class:`DockerSandbox` — opt-in. Shells out to ``docker run`` with
  ``--memory``, ``--cpus``, ``--network=none`` (unless
  ``network=true``), ``--read-only``, and a single bind-mounted
  writable scratch directory. No dependency on ``docker-py``.

Permission policy (``agentbus.chat._permissions``) is evaluated
**above** the sandbox — a denied command short-circuits before the
sandbox ever runs. The sandbox is the last line of defence against
anything a policy or the LLM itself let through.

Platform notes:

* ``RLIMIT_AS`` is enforced by the Linux kernel but is known to be
  unreliable on macOS; several variants silently ignore it. We still
  apply it (it's free when honoured, harmless when not), but on
  Darwin you should prefer the docker backend for real isolation.
* Network isolation is ``docker`` only — subprocess namespaces would
  require root and are outside the scope of this layer.
* ``preexec_fn`` is Unix-only. AgentBus is Unix-only per the PRD.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# ── defaults ──────────────────────────────────────────────────────────────

DEFAULT_CPU_SECONDS = 30
DEFAULT_MEMORY_MB = 512
DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024  # 256 KiB
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
DEFAULT_DOCKER_IMAGE = "python:3.12-slim"

_TRUNC_NOTE = "\n[output truncated at {limit} bytes]"


# ── config ────────────────────────────────────────────────────────────────


@dataclass
class SandboxConfig:
    """Sandbox settings loaded from the ``sandbox:`` section of ``agentbus.yaml``.

    Unspecified fields fall back to conservative defaults so the feature
    is secure-by-default even when the YAML block is missing entirely.
    """

    backend: str = "subprocess"  # or "docker"
    cpu_seconds: int = DEFAULT_CPU_SECONDS
    memory_mb: int = DEFAULT_MEMORY_MB
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    workdir: str | None = None  # None = per-invocation tempdir
    env_passthrough: list[str] = field(default_factory=list)
    # docker-only
    image: str = DEFAULT_DOCKER_IMAGE
    network: bool = False


@dataclass
class SandboxResult:
    """Normalized outcome of a sandboxed invocation."""

    output: str
    exit_code: int
    timed_out: bool = False
    truncated: bool = False

    def to_tool_output(self) -> str:
        """Render the LLM-facing string for a tool handler."""
        text = self.output
        if self.timed_out:
            return f"Error: command timed out\n{text}" if text else "Error: command timed out"
        return text.strip() or "(no output)"


# ── protocol ──────────────────────────────────────────────────────────────


class Sandbox(Protocol):
    """Common surface for every backend."""

    async def run_shell(self, command: str, timeout: float) -> SandboxResult: ...

    async def run_python(self, code: str, timeout: float) -> SandboxResult: ...


# ── subprocess backend ────────────────────────────────────────────────────


def _preexec_limits(cpu_seconds: int, memory_mb: int):
    """Return a preexec_fn closure that applies rlimits in the child.

    Deferred import of ``resource`` keeps the module importable on
    Windows (where ``resource`` doesn't exist) — the subprocess backend
    won't work there, but the config parser and tests that don't
    actually execute commands still do.
    """

    def _apply() -> None:
        import resource  # Unix-only; deliberately inside the closure

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        mem_bytes = memory_mb * 1024 * 1024
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        os.setsid()

    return _apply


def _scrub_env(passthrough: list[str]) -> dict[str, str]:
    allowed = set(DEFAULT_ENV_ALLOWLIST) | set(passthrough)
    return {k: v for k, v in os.environ.items() if k in allowed}


class SubprocessSandbox:
    """Default sandbox: rlimits + scrubbed env + workdir confinement."""

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config

    async def run_shell(self, command: str, timeout: float) -> SandboxResult:
        return await self._run(
            ["/bin/sh", "-c", command],
            timeout=timeout,
        )

    async def run_python(self, code: str, timeout: float) -> SandboxResult:
        return await self._run(
            ["python3", "-I", "-c", code],
            timeout=timeout,
        )

    async def _run(self, argv: list[str], *, timeout: float) -> SandboxResult:
        workdir, temp = self._pick_workdir()
        env = _scrub_env(self._config.env_passthrough)
        env.setdefault("TMPDIR", workdir)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=workdir,
                env=env,
                preexec_fn=_preexec_limits(
                    self._config.cpu_seconds,
                    self._config.memory_mb,
                ),
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                self._kill(proc)
                return SandboxResult(
                    output=f"(terminated after {timeout:.0f}s)",
                    exit_code=-1,
                    timed_out=True,
                )
            text, truncated = self._truncate(stdout)
            return SandboxResult(
                output=text,
                exit_code=proc.returncode or 0,
                truncated=truncated,
            )
        finally:
            if temp:
                shutil.rmtree(workdir, ignore_errors=True)

    def _pick_workdir(self) -> tuple[str, bool]:
        """Return ``(path, is_temporary)``. Caller deletes when temp."""
        if self._config.workdir:
            Path(self._config.workdir).mkdir(parents=True, exist_ok=True)
            return self._config.workdir, False
        return tempfile.mkdtemp(prefix="agentbus-sandbox-"), True

    def _kill(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), 9)

    def _truncate(self, data: bytes) -> tuple[str, bool]:
        limit = self._config.max_output_bytes
        if len(data) <= limit:
            return data.decode(errors="replace"), False
        note = _TRUNC_NOTE.format(limit=limit)
        return data[:limit].decode(errors="replace") + note, True


# ── docker backend ────────────────────────────────────────────────────────


class DockerSandbox:
    """Opt-in sandbox: each invocation runs inside a fresh docker container.

    The container is network-disabled (unless ``network=true``),
    read-only on the rootfs, and given one writable scratch directory
    bind-mounted at ``/workspace`` (also the working directory). The
    caller's workdir on the host is ``config.workdir`` if set, else a
    per-invocation tempdir that is removed after the container exits.
    """

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        if not shutil.which("docker"):
            raise RuntimeError(
                "sandbox.backend=docker but the `docker` binary was not found on PATH"
            )

    async def run_shell(self, command: str, timeout: float) -> SandboxResult:
        return await self._run(["/bin/sh", "-c", command], timeout=timeout)

    async def run_python(self, code: str, timeout: float) -> SandboxResult:
        return await self._run(["python3", "-I", "-c", code], timeout=timeout)

    async def _run(self, inner_argv: list[str], *, timeout: float) -> SandboxResult:
        workdir, temp = self._pick_workdir()
        try:
            docker_argv = [*self._docker_argv(workdir), self._config.image, *inner_argv]
            proc = await asyncio.create_subprocess_exec(
                *docker_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
            except TimeoutError:
                proc.kill()
                return SandboxResult(
                    output=f"(terminated after {timeout:.0f}s)",
                    exit_code=-1,
                    timed_out=True,
                )
            text, truncated = self._truncate(stdout)
            return SandboxResult(
                output=text,
                exit_code=proc.returncode or 0,
                truncated=truncated,
            )
        finally:
            if temp:
                shutil.rmtree(workdir, ignore_errors=True)

    def _docker_argv(self, host_workdir: str) -> list[str]:
        argv = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--read-only",
            "--memory",
            f"{self._config.memory_mb}m",
            "--cpus",
            f"{self._config.cpu_seconds / 30.0:.2f}",
            "-v",
            f"{host_workdir}:/workspace:rw",
            "-w",
            "/workspace",
        ]
        if not self._config.network:
            argv += ["--network", "none"]
        for var in self._config.env_passthrough:
            if var in os.environ:
                argv += ["-e", f"{var}={os.environ[var]}"]
        return argv

    def _pick_workdir(self) -> tuple[str, bool]:
        if self._config.workdir:
            Path(self._config.workdir).mkdir(parents=True, exist_ok=True)
            return self._config.workdir, False
        return tempfile.mkdtemp(prefix="agentbus-sandbox-"), True

    def _truncate(self, data: bytes) -> tuple[str, bool]:
        limit = self._config.max_output_bytes
        if len(data) <= limit:
            return data.decode(errors="replace"), False
        note = _TRUNC_NOTE.format(limit=limit)
        return data[:limit].decode(errors="replace") + note, True


# ── loader + factory ──────────────────────────────────────────────────────


def load_sandbox_from_dict(data: dict[str, Any] | None) -> SandboxConfig:
    """Parse the ``sandbox:`` block from ``agentbus.yaml``.

    Returns a :class:`SandboxConfig` with defaults applied.
    ``None`` or ``{}`` → default subprocess sandbox with conservative
    limits. Unknown ``backend`` values raise :class:`ValueError`.
    """
    if not data:
        return SandboxConfig()
    if not isinstance(data, dict):
        raise ValueError(f"sandbox: expected a mapping, got {type(data).__name__}")

    backend = data.get("backend", "subprocess")
    if backend not in ("subprocess", "docker"):
        raise ValueError(f"sandbox.backend: must be 'subprocess' or 'docker', got {backend!r}")

    passthrough_raw = data.get("env_passthrough", [])
    if not isinstance(passthrough_raw, list):
        raise ValueError("sandbox.env_passthrough: must be a list of environment variable names")

    return SandboxConfig(
        backend=backend,
        cpu_seconds=int(data.get("cpu_seconds", DEFAULT_CPU_SECONDS)),
        memory_mb=int(data.get("memory_mb", DEFAULT_MEMORY_MB)),
        max_output_bytes=int(data.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)),
        workdir=data.get("workdir"),
        env_passthrough=[str(v) for v in passthrough_raw],
        image=str(data.get("image", DEFAULT_DOCKER_IMAGE)),
        network=bool(data.get("network", False)),
    )


def build_sandbox(config: SandboxConfig) -> Sandbox:
    """Instantiate the backend selected in ``config.backend``."""
    if config.backend == "docker":
        return DockerSandbox(config)
    return SubprocessSandbox(config)


__all__ = [
    "DEFAULT_CPU_SECONDS",
    "DEFAULT_DOCKER_IMAGE",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MEMORY_MB",
    "DockerSandbox",
    "Sandbox",
    "SandboxConfig",
    "SandboxResult",
    "SubprocessSandbox",
    "build_sandbox",
    "load_sandbox_from_dict",
]
