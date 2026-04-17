"""Daemon-mode entry points for `agentbus daemon`.

Keeps the process in the foreground so systemd/launchd can manage
lifecycle themselves (Type=simple / KeepAlive). The daemon writes an
advisory-locked pidfile at startup so a second instance can't race it,
handles SIGTERM gracefully via the bus's ``install_signal_handlers``
path, and exposes ``status`` / ``stop`` helpers that inspect the pidfile
without needing the Unix introspection socket.

The ``emit_systemd_unit`` and ``emit_launchd_plist`` helpers render
service-file templates with the absolute path of the current interpreter
+ ``agentbus`` binary baked in — so copying the emitted file to
``~/.config/systemd/user/`` or ``~/Library/LaunchAgents/`` is enough to
wire up auto-start.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agentbus.launch import launch_sync

DEFAULT_PID_PATH = Path.home() / ".agentbus" / "agentbus.pid"
DEFAULT_LOG_PATH = Path.home() / ".agentbus" / "agentbus.log"

logger = logging.getLogger(__name__)


# ── pidfile helpers ──────────────────────────────────────────────────────────


class PidfileLockedError(RuntimeError):
    """Another process already holds the daemon pidfile."""


def _acquire_pidfile(path: Path) -> int:
    """Create / open the pidfile and take an exclusive advisory lock.

    Returns the open fd. The caller must keep it alive for the whole
    daemon lifetime — when the process exits, the kernel releases the
    lock automatically.

    Raises :class:`PidfileLockedError` if another live process already
    holds the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            existing = read_pidfile(path)
            raise PidfileLockedError(f"pidfile {path} is locked by pid={existing or '?'}") from None
        raise

    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return fd


def _release_pidfile(fd: int, path: Path) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)
    # Best-effort cleanup — don't unlink if another daemon has already
    # taken the lock (that would race).
    with contextlib.suppress(OSError):
        if read_pidfile(path) == os.getpid():
            path.unlink(missing_ok=True)


def read_pidfile(path: Path) -> int | None:
    """Return the PID written in the pidfile, or None if missing/empty."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not content:
        return None
    try:
        return int(content.split()[0])
    except ValueError:
        return None


def is_process_alive(pid: int) -> bool:
    """Return True if `pid` refers to a live process we can signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ── status / stop ────────────────────────────────────────────────────────────


@dataclass
class DaemonStatus:
    pid: int | None
    running: bool
    pidfile: Path

    def describe(self) -> str:
        if self.running and self.pid:
            return f"running (pid={self.pid}, pidfile={self.pidfile})"
        if self.pid:
            return f"stale pidfile (pid={self.pid} not running, pidfile={self.pidfile})"
        return f"not running (no pidfile at {self.pidfile})"


def status(pidfile: Path = DEFAULT_PID_PATH) -> DaemonStatus:
    pid = read_pidfile(pidfile)
    alive = is_process_alive(pid) if pid else False
    return DaemonStatus(pid=pid, running=alive, pidfile=pidfile)


def stop(
    pidfile: Path = DEFAULT_PID_PATH,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.2,
) -> bool:
    """Send SIGTERM to the daemon and wait for it to exit.

    Returns True if the process exited within ``timeout``. Returns False
    if no daemon was running or if it didn't exit in time.
    """
    pid = read_pidfile(pidfile)
    if not pid or not is_process_alive(pid):
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(poll_interval)
    return False


# ── run ──────────────────────────────────────────────────────────────────────


def run(
    config_path: str | Path,
    *,
    pidfile: Path = DEFAULT_PID_PATH,
) -> int:
    """Run the bus in the foreground with pidfile locking + signal handling.

    Returns a POSIX-style exit code: 0 on clean shutdown, 1 on error,
    2 if the pidfile was already locked.
    """
    pidfile = Path(pidfile)
    try:
        pid_fd = _acquire_pidfile(pidfile)
    except PidfileLockedError as exc:
        print(f"agentbus daemon: {exc}", file=sys.stderr)
        return 2

    logger.info("daemon started pid=%d config=%s pidfile=%s", os.getpid(), config_path, pidfile)
    try:
        launch_sync(config_path)
    except KeyboardInterrupt:
        logger.info("daemon interrupted")
    except Exception:
        logger.exception("daemon crashed")
        return 1
    finally:
        _release_pidfile(pid_fd, pidfile)
        logger.info("daemon exited")
    return 0


# ── service-file templates ───────────────────────────────────────────────────


def _agentbus_command() -> str:
    """Best-effort absolute path to the agentbus entry point."""
    resolved = shutil.which("agentbus")
    if resolved:
        return resolved
    # Fallback: invoke the module via the current interpreter.
    return f"{sys.executable} -m agentbus"


def emit_systemd_unit(config_path: str | Path) -> str:
    """Render a user-level systemd unit file for `agentbus daemon`."""
    config_abs = Path(config_path).expanduser().resolve()
    cmd = _agentbus_command()
    log_file = DEFAULT_LOG_PATH
    return f"""\
[Unit]
Description=AgentBus message bus
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={cmd} daemon start {config_abs}
Restart=on-failure
RestartSec=3
Environment=AGENTBUS_LOG_FORMAT=json
Environment=AGENTBUS_LOG_FILE={log_file}
# Graceful-shutdown window must exceed bus drain_timeout.
TimeoutStopSec=15
KillSignal=SIGTERM

[Install]
WantedBy=default.target
"""


def emit_launchd_plist(config_path: str | Path, *, label: str = "com.agentbus.daemon") -> str:
    """Render a macOS LaunchAgent plist for `agentbus daemon`."""
    config_abs = Path(config_path).expanduser().resolve()
    cmd = _agentbus_command()
    log_file = DEFAULT_LOG_PATH
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{cmd}</string>
        <string>daemon</string>
        <string>start</string>
        <string>{config_abs}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>AGENTBUS_LOG_FORMAT</key>
        <string>json</string>
    </dict>
</dict>
</plist>
"""


__all__ = [
    "DEFAULT_LOG_PATH",
    "DEFAULT_PID_PATH",
    "DaemonStatus",
    "PidfileLockedError",
    "emit_launchd_plist",
    "emit_systemd_unit",
    "is_process_alive",
    "read_pidfile",
    "run",
    "status",
    "stop",
]
