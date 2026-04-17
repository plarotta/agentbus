"""Structured logging for agentbus.

Design:
  * One root logger: ``agentbus``. Child loggers flow from it
    (``agentbus.bus``, ``agentbus.node.<name>``, ``agentbus.harness``, ...).
  * ``setup_logging()`` installs a single stream handler with either a JSON
    or human-text formatter. Idempotent — calling twice replaces the handler.
  * Correlation IDs flow via a ``contextvars.ContextVar`` so async tasks
    inherit them. ``MessageBus`` sets the ID before every ``on_message``
    dispatch, so any ``logger.info(...)`` fired inside a handler is
    automatically tagged with the request/reply correlation ID.
  * Environment controls (no code changes needed for common ops):
      - ``AGENTBUS_LOG_LEVEL``   — DEBUG | INFO | WARNING | ERROR (default INFO)
      - ``AGENTBUS_LOG_FORMAT``  — ``text`` | ``json`` (default ``text``)
      - ``AGENTBUS_LOG_FILE``    — path; if set, logs go here instead of stderr

Nothing is logged until ``setup_logging()`` is called. The library only
emits records — it's up to the CLI / embedder to configure the sink.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
from typing import IO, Any

# ── Correlation ID context ───────────────────────────────────────────────────

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agentbus_correlation_id", default=None
)


def set_correlation_id(cid: str | None) -> contextvars.Token[str | None]:
    """Set the correlation ID for the current async task.

    Returns a token that can be passed to :func:`reset_correlation_id` to
    restore the previous value. Typically used as a pair around a dispatch:

        token = set_correlation_id(msg.correlation_id)
        try:
            await node.on_message(msg)
        finally:
            reset_correlation_id(token)
    """
    return _correlation_id.set(cid)


def reset_correlation_id(token: contextvars.Token[str | None]) -> None:
    _correlation_id.reset(token)


def current_correlation_id() -> str | None:
    return _correlation_id.get()


# ── Formatters / filters ─────────────────────────────────────────────────────


# Keys that exist on every LogRecord and should never leak into the extras
# payload — copied from the CPython source at stdlib/logging/__init__.py.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "correlation_id",
    }
)


class _CorrelationIdFilter(logging.Filter):
    """Attach the current correlation ID (if any) to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get()
        return True


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per line.

    Includes: ``ts`` (RFC3339 UTC), ``level``, ``logger``, ``msg``,
    ``correlation_id`` (if set), ``exc_info`` (if present), and any
    ``extra={...}`` keys the caller passed that are JSON-serializable.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z"
        )
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = getattr(record, "correlation_id", None)
        if cid:
            payload["correlation_id"] = cid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        for key, val in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(val)
                payload[key] = val
            except (TypeError, ValueError):
                payload[key] = repr(val)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable one-line format with a truncated correlation ID.

    Example:
        14:22:01 INFO  [a1b2c3d4] agentbus.bus: Registering topic /inbound
    """

    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", None)
        cid_tag = f"[{cid[:8]}]" if cid else "[--------]"
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        msg = record.getMessage()
        line = f"{ts} {record.levelname:<5s} {cid_tag} {record.name}: {msg}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# ── Public setup API ─────────────────────────────────────────────────────────


def _resolve_stream() -> IO[str]:
    path = os.environ.get("AGENTBUS_LOG_FILE")
    if path:
        # Line-buffered so tailers see records immediately.
        return open(path, "a", encoding="utf-8", buffering=1)
    return sys.stderr


def setup_logging(
    *,
    level: str | int | None = None,
    format: str | None = None,
    stream: IO[str] | None = None,
) -> None:
    """Configure the ``agentbus`` logger tree. Idempotent.

    Precedence: explicit arguments > ``AGENTBUS_LOG_*`` env vars > defaults.
    Defaults: level=INFO, format=text, stream=stderr (or ``AGENTBUS_LOG_FILE``
    if set).
    """
    level = level or os.environ.get("AGENTBUS_LOG_LEVEL", "INFO")
    fmt = (format or os.environ.get("AGENTBUS_LOG_FORMAT", "text")).lower()
    if fmt not in {"text", "json"}:
        raise ValueError(f"AGENTBUS_LOG_FORMAT must be 'text' or 'json', got {fmt!r}")

    root = logging.getLogger("agentbus")
    for handler in list(root.handlers):
        root.removeHandler(handler)

    sink = stream if stream is not None else _resolve_stream()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(JSONFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(_CorrelationIdFilter())
    root.addHandler(handler)
    root.setLevel(level)
    # Don't double-log through the root logger if something else configured it.
    root.propagate = False


def node_logger(name: str) -> logging.Logger:
    """Return the child logger for a named node: ``agentbus.node.<name>``."""
    return logging.getLogger(f"agentbus.node.{name}")


__all__ = [
    "JSONFormatter",
    "TextFormatter",
    "current_correlation_id",
    "node_logger",
    "reset_correlation_id",
    "set_correlation_id",
    "setup_logging",
]
