"""Tests for `agentbus.daemon` — pidfile locking, status, stop, templates."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agentbus import daemon


def test_acquire_pidfile_writes_pid(tmp_path):
    pidfile = tmp_path / "agentbus.pid"
    fd = daemon._acquire_pidfile(pidfile)
    try:
        assert pidfile.exists()
        assert daemon.read_pidfile(pidfile) == os.getpid()
    finally:
        daemon._release_pidfile(fd, pidfile)


def test_release_pidfile_removes_file(tmp_path):
    pidfile = tmp_path / "agentbus.pid"
    fd = daemon._acquire_pidfile(pidfile)
    daemon._release_pidfile(fd, pidfile)
    assert not pidfile.exists()


def test_acquire_pidfile_second_locker_is_denied(tmp_path):
    pidfile = tmp_path / "agentbus.pid"
    fd = daemon._acquire_pidfile(pidfile)
    try:
        with pytest.raises(daemon.PidfileLockedError, match="locked"):
            daemon._acquire_pidfile(pidfile)
    finally:
        daemon._release_pidfile(fd, pidfile)


def test_read_pidfile_missing_returns_none(tmp_path):
    assert daemon.read_pidfile(tmp_path / "missing.pid") is None


def test_read_pidfile_empty_returns_none(tmp_path):
    pidfile = tmp_path / "empty.pid"
    pidfile.write_text("", encoding="utf-8")
    assert daemon.read_pidfile(pidfile) is None


def test_read_pidfile_malformed_returns_none(tmp_path):
    pidfile = tmp_path / "bad.pid"
    pidfile.write_text("not-a-pid\n", encoding="utf-8")
    assert daemon.read_pidfile(pidfile) is None


def test_is_process_alive_for_current_pid():
    assert daemon.is_process_alive(os.getpid()) is True


def test_is_process_alive_for_dead_pid():
    # PID 0 is the scheduler on POSIX; os.kill(0, 0) is special-cased.
    # Use a very large unlikely PID instead.
    assert daemon.is_process_alive(999_999_999) is False


def test_status_reports_running_for_our_pid(tmp_path):
    pidfile = tmp_path / "agentbus.pid"
    fd = daemon._acquire_pidfile(pidfile)
    try:
        st = daemon.status(pidfile)
        assert st.pid == os.getpid()
        assert st.running is True
        assert "running" in st.describe()
    finally:
        daemon._release_pidfile(fd, pidfile)


def test_status_reports_not_running_when_missing(tmp_path):
    st = daemon.status(tmp_path / "nope.pid")
    assert st.pid is None
    assert st.running is False
    assert "not running" in st.describe()


def test_status_detects_stale_pidfile(tmp_path):
    pidfile = tmp_path / "stale.pid"
    pidfile.write_text("999999999\n", encoding="utf-8")
    st = daemon.status(pidfile)
    assert st.pid == 999_999_999
    assert st.running is False
    assert "stale" in st.describe()


def test_stop_returns_false_when_no_daemon(tmp_path):
    assert daemon.stop(tmp_path / "none.pid", timeout=0.1) is False


# ── Integration: stop() against a live subprocess ────────────────────────────


_CHILD_SCRIPT = textwrap.dedent(
    """
    import os, signal, sys, time
    from pathlib import Path
    from agentbus import daemon

    pidfile = Path(sys.argv[1])
    fd = daemon._acquire_pidfile(pidfile)

    def _on_term(signum, frame):
        daemon._release_pidfile(fd, pidfile)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    time.sleep(15.0)
    """
).strip()


def test_stop_sends_sigterm_and_waits():
    pidfile_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    pidfile = pidfile_dir / "agentbus.pid"

    proc = subprocess.Popen([sys.executable, "-c", _CHILD_SCRIPT, str(pidfile)])
    # Reap the subprocess concurrently — otherwise a zombie keeps
    # is_process_alive() returning True and stop() times out.
    reaper_exit: list[int | None] = [None]

    def _reap():
        try:
            reaper_exit[0] = proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            reaper_exit[0] = -1

    reaper = threading.Thread(target=_reap, daemon=True)
    reaper.start()

    try:
        # Wait for the child to write the pidfile.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if daemon.read_pidfile(pidfile) == proc.pid:
                break
            time.sleep(0.05)
        assert daemon.read_pidfile(pidfile) == proc.pid

        ok = daemon.stop(pidfile, timeout=5.0)
        reaper.join(timeout=5.0)
        assert ok is True
        assert reaper_exit[0] == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1.0)


# ── Templates ────────────────────────────────────────────────────────────────


def test_emit_systemd_unit_contains_config_and_command(tmp_path):
    cfg = tmp_path / "agentbus.yaml"
    cfg.write_text("provider: ollama\n", encoding="utf-8")
    unit = daemon.emit_systemd_unit(cfg)
    assert "[Service]" in unit
    assert "Type=simple" in unit
    assert str(cfg.resolve()) in unit
    assert "daemon start" in unit
    assert "KillSignal=SIGTERM" in unit


def test_emit_launchd_plist_is_valid_plist(tmp_path):
    import plistlib

    cfg = tmp_path / "agentbus.yaml"
    cfg.write_text("provider: ollama\n", encoding="utf-8")
    plist = daemon.emit_launchd_plist(cfg, label="com.test.agentbus")
    parsed = plistlib.loads(plist.encode("utf-8"))
    assert parsed["Label"] == "com.test.agentbus"
    assert "daemon" in parsed["ProgramArguments"]
    assert "start" in parsed["ProgramArguments"]
    assert str(cfg.resolve()) in parsed["ProgramArguments"]
    assert parsed["RunAtLoad"] is True


# ── CLI wiring ───────────────────────────────────────────────────────────────


def test_cli_daemon_install_systemd(tmp_path, capsys):
    from agentbus.cli import app

    cfg = tmp_path / "agentbus.yaml"
    cfg.write_text("provider: ollama\n", encoding="utf-8")
    rc = app(["daemon", "install", "systemd", str(cfg)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "[Service]" in captured.out


def test_cli_daemon_install_launchd(tmp_path, capsys):
    from agentbus.cli import app

    cfg = tmp_path / "agentbus.yaml"
    cfg.write_text("provider: ollama\n", encoding="utf-8")
    rc = app(["daemon", "install", "launchd", str(cfg), "--label", "com.x.y"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "com.x.y" in captured.out
    assert "<plist" in captured.out


def test_cli_daemon_status_no_daemon(tmp_path, capsys):
    from agentbus.cli import app

    pidfile = tmp_path / "no.pid"
    rc = app(["daemon", "status", "--pidfile", str(pidfile)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "not running" in captured.out
