"""Tests for agentbus.logging_config."""

from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest

from agentbus.logging_config import (
    JSONFormatter,
    TextFormatter,
    current_correlation_id,
    node_logger,
    reset_correlation_id,
    set_correlation_id,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _reset_agentbus_logger():
    """Isolate every test: detach all handlers from the ``agentbus`` logger."""
    yield
    logger = logging.getLogger("agentbus")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


# ── correlation ID context ──────────────────────────────────────────────────


def test_correlation_id_roundtrip():
    assert current_correlation_id() is None
    token = set_correlation_id("abc-123")
    try:
        assert current_correlation_id() == "abc-123"
    finally:
        reset_correlation_id(token)
    assert current_correlation_id() is None


def test_correlation_id_isolated_per_task():
    """Each asyncio task gets its own contextvars copy."""

    seen: dict[str, str | None] = {}

    async def worker(name: str, cid: str) -> None:
        set_correlation_id(cid)
        await asyncio.sleep(0)  # yield — interleave with sibling
        seen[name] = current_correlation_id()

    async def driver() -> None:
        await asyncio.gather(worker("a", "cid-a"), worker("b", "cid-b"))

    asyncio.run(driver())
    assert seen == {"a": "cid-a", "b": "cid-b"}


# ── JSON formatter ──────────────────────────────────────────────────────────


def _make_record(
    *,
    name: str = "agentbus.test",
    level: int = logging.INFO,
    msg: str = "hello",
    extra: dict | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


def test_json_formatter_emits_required_keys():
    rec = _make_record(msg="hi %s", extra={"args": ("world",)})
    # Reset args so getMessage() interpolates properly
    rec.args = ("world",)
    out = JSONFormatter().format(rec)
    obj = json.loads(out)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "agentbus.test"
    assert obj["msg"] == "hi world"
    assert "ts" in obj and obj["ts"].endswith("Z")


def test_json_formatter_includes_correlation_id_when_present():
    rec = _make_record(extra={"correlation_id": "cid-xyz"})
    obj = json.loads(JSONFormatter().format(rec))
    assert obj["correlation_id"] == "cid-xyz"


def test_json_formatter_omits_correlation_id_when_absent():
    rec = _make_record(extra={"correlation_id": None})
    obj = json.loads(JSONFormatter().format(rec))
    assert "correlation_id" not in obj


def test_json_formatter_includes_extras():
    rec = _make_record(extra={"correlation_id": None, "request_id": 42, "topic": "/t"})
    obj = json.loads(JSONFormatter().format(rec))
    assert obj["request_id"] == 42
    assert obj["topic"] == "/t"


def test_json_formatter_handles_unserializable_extras():
    class _Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    rec = _make_record(extra={"correlation_id": None, "obj": _Opaque()})
    obj = json.loads(JSONFormatter().format(rec))
    assert obj["obj"] == "<opaque>"


# ── Text formatter ──────────────────────────────────────────────────────────


def test_text_formatter_basic_shape():
    rec = _make_record(msg="started", extra={"correlation_id": "abcdefghijk"})
    line = TextFormatter().format(rec)
    assert "INFO" in line
    assert "agentbus.test" in line
    assert "started" in line
    assert "[abcdefgh]" in line  # truncated to 8 chars


def test_text_formatter_dashes_when_no_correlation_id():
    rec = _make_record(extra={"correlation_id": None})
    line = TextFormatter().format(rec)
    assert "[--------]" in line


# ── setup_logging ───────────────────────────────────────────────────────────


def test_setup_logging_attaches_single_handler():
    stream = io.StringIO()
    setup_logging(level="DEBUG", format="text", stream=stream)
    logger = logging.getLogger("agentbus")
    assert len(logger.handlers) == 1

    # Calling again replaces the handler rather than duplicating.
    setup_logging(level="DEBUG", format="text", stream=stream)
    assert len(logger.handlers) == 1


def test_setup_logging_records_flow_to_stream_with_correlation_id():
    stream = io.StringIO()
    setup_logging(level="DEBUG", format="json", stream=stream)

    token = set_correlation_id("cid-42")
    try:
        logging.getLogger("agentbus.bus").info("ping")
    finally:
        reset_correlation_id(token)

    line = stream.getvalue().strip()
    obj = json.loads(line)
    assert obj["msg"] == "ping"
    assert obj["logger"] == "agentbus.bus"
    assert obj["correlation_id"] == "cid-42"


def test_setup_logging_rejects_invalid_format():
    with pytest.raises(ValueError, match="text"):
        setup_logging(format="yaml", stream=io.StringIO())


def test_setup_logging_respects_env_defaults(monkeypatch):
    monkeypatch.setenv("AGENTBUS_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("AGENTBUS_LOG_FORMAT", "json")
    stream = io.StringIO()
    setup_logging(stream=stream)
    logger = logging.getLogger("agentbus")
    assert logger.level == logging.WARNING
    logger.warning("boom")
    obj = json.loads(stream.getvalue().strip())
    assert obj["level"] == "WARNING"


# ── node_logger ─────────────────────────────────────────────────────────────


def test_node_logger_name():
    assert node_logger("planner").name == "agentbus.node.planner"


def test_node_logger_is_child_of_agentbus_root():
    stream = io.StringIO()
    setup_logging(level="DEBUG", format="text", stream=stream)
    node_logger("tool_runner").info("dispatched")
    assert "agentbus.node.tool_runner" in stream.getvalue()
    assert "dispatched" in stream.getvalue()
