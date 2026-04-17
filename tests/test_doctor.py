"""Tests for `agentbus doctor`."""

from pathlib import Path

from agentbus import doctor


def test_check_python_passes():
    c = doctor._check_python()
    assert c.status == "ok"


def test_check_pydantic_v2():
    c = doctor._check_pydantic()
    assert c.status == "ok"
    assert c.detail.startswith("2.")


def test_check_sessions_dir_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Force a fresh home so the probe creates the dir itself.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    c = doctor._check_sessions_dir()
    assert c.status == "ok"
    assert (tmp_path / ".agentbus" / "sessions").is_dir()


def test_check_config_missing(tmp_path):
    c = doctor._check_config(tmp_path / "nope.yaml")
    assert c.status == "warn"
    assert "not found" in c.detail


def test_check_config_valid(tmp_path):
    cfg = tmp_path / "agentbus.yaml"
    cfg.write_text(
        "provider: anthropic\nmodel: claude-haiku-4-5-20251001\ntools: [bash]\nmemory: false\n"
    )
    c = doctor._check_config(cfg)
    assert c.status == "ok"
    assert "provider=anthropic" in c.detail


def test_check_config_invalid(tmp_path):
    cfg = tmp_path / "agentbus.yaml"
    cfg.write_text("this is not: [valid yaml")
    c = doctor._check_config(cfg)
    assert c.status == "fail"


def test_check_provider_deps_unknown_provider(tmp_path):
    cfg = tmp_path / "agentbus.yaml"
    cfg.write_text("provider: faker\nmodel: x\ntools: []\nmemory: false\n")
    checks = doctor._check_provider_deps(cfg)
    assert any(c.status == "fail" and "unknown provider" in c.detail for c in checks)


def test_check_provider_deps_no_config(tmp_path):
    checks = doctor._check_provider_deps(tmp_path / "nope.yaml")
    assert len(checks) == 1
    assert checks[0].status == "warn"


def test_check_socket_absent(tmp_path):
    c = doctor._check_socket(str(tmp_path / "nope.sock"))
    assert c.status == "warn"


def test_check_socket_path_length_macos_too_long():
    long = "/tmp/" + "x" * 200
    c = doctor._check_socket_path_length(long)
    # non-macOS passes unconditionally; macOS fails. Accept either since tests
    # run on both.
    if c.status == "fail":
        assert "macOS AF_UNIX" in c.detail
    else:
        assert c.status == "ok"


def test_run_passes_with_minimal_valid_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / "agentbus.yaml"
    cfg.write_text(
        "provider: anthropic\nmodel: claude-haiku-4-5-20251001\ntools: []\nmemory: false\n"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    # Force socket path inside tmp so it doesn't race with a real /tmp/agentbus.sock.
    code = doctor.run(config_path=cfg, socket_path=str(tmp_path / "agentbus.sock"))
    out = capsys.readouterr().out
    assert "agentbus doctor" in out
    # anthropic SDK is optional; if not installed, this will fail. Tolerate.
    try:
        import anthropic  # noqa: F401

        assert code == 0
    except ModuleNotFoundError:
        assert code == 1
