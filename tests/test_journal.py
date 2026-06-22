"""Tests for the durable SQLite journal (LogStore)."""

import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel

from agentbus.contracts import register_schema
from agentbus.core import BusCore, LogStore
from agentbus.journal import SqliteLog
from agentbus.message import Message


class _P(BaseModel):
    n: int


@pytest.fixture
def schemas() -> dict:
    reg: dict[str, type[BaseModel]] = {}
    register_schema("/a", _P, registry=reg)
    register_schema("/b", _P, registry=reg)
    return reg


@pytest.fixture
def db_path():
    d = tempfile.mkdtemp(dir="/tmp")
    yield Path(d) / "journal.db"


def _msg(topic: str, n: int) -> Message:
    return Message(source_node="t", topic=topic, payload=_P(n=n))


def test_satisfies_logstore_protocol(db_path, schemas):
    log = SqliteLog(db_path, schemas=schemas)
    assert isinstance(log, LogStore)
    log.close()


def test_append_stamps_monotonic_offsets(db_path, schemas):
    log = SqliteLog(db_path, schemas=schemas)
    a = log.append(_msg("/a", 1))
    b = log.append(_msg("/a", 2))
    assert a.offset == 1
    assert b.offset == 2
    assert log.latest_offset() == 2
    log.close()


def test_read_from_offset_exclusive(db_path, schemas):
    log = SqliteLog(db_path, schemas=schemas)
    for i in range(5):
        log.append(_msg("/a", i))
    out = list(log.read(from_offset=3))
    assert [m.payload.n for m in out] == [3, 4]
    assert [m.offset for m in out] == [4, 5]
    log.close()


def test_read_filters_by_topic_pattern(db_path, schemas):
    log = SqliteLog(db_path, schemas=schemas)
    log.append(_msg("/a", 1))
    log.append(_msg("/b", 2))
    log.append(_msg("/a", 3))
    out = list(log.read(topic="/a"))
    assert [m.payload.n for m in out] == [1, 3]
    log.close()


def test_read_respects_limit(db_path, schemas):
    log = SqliteLog(db_path, schemas=schemas)
    for i in range(5):
        log.append(_msg("/a", i))
    out = list(log.read(limit=2))
    assert [m.payload.n for m in out] == [0, 1]
    log.close()


def test_durable_across_reopen(db_path, schemas):
    log = SqliteLog(db_path, schemas=schemas)
    log.append(_msg("/a", 1))
    log.append(_msg("/a", 2))
    log.close()

    # Simulate a restart: new process, new SqliteLog over the same file.
    reopened = SqliteLog(db_path, schemas=schemas)
    out = list(reopened.read(from_offset=0))
    assert [m.payload.n for m in out] == [1, 2]
    assert [m.offset for m in out] == [1, 2]
    # Offsets keep climbing — never reused after restart.
    assert reopened.append(_msg("/a", 3)).offset == 3
    assert reopened.latest_offset() == 3
    reopened.close()


def test_offsets_monotonic_after_prune(db_path, schemas):
    log = SqliteLog(db_path, schemas=schemas)
    for i in range(3):
        log.append(_msg("/a", i))
    removed = log.prune(before_offset=3)  # drop offsets 1, 2
    assert removed == 2
    assert [m.offset for m in log.read(from_offset=0)] == [3]
    # New appends continue past the high-water, not reusing 1/2.
    assert log.append(_msg("/a", 9)).offset == 4
    log.close()


def test_skip_on_schema_drift(db_path, schemas):
    # Write a row under schema _P, then read with a registry where /a's schema
    # has changed to something the stored payload can't satisfy.
    log = SqliteLog(db_path, schemas=schemas)
    log.append(_msg("/a", 1))
    log.append(_msg("/b", 2))
    log.close()

    class _Strict(BaseModel):
        required_field: str  # the stored {"n": 1} payload won't validate

    drifted = {"/a": _Strict, "/b": _P}
    reopened = SqliteLog(db_path, schemas=drifted)
    out = list(reopened.read(from_offset=0))
    # /a entry skipped (drift); /b entry survives.
    assert [m.topic for m in out] == ["/b"]
    reopened.close()


def test_buscore_with_durable_store(db_path, schemas):
    log = SqliteLog(db_path, schemas=schemas)
    core = BusCore(schemas=schemas, log_store=log)
    core.publish(_msg("/a", 1))
    core.publish(_msg("/a", 2))
    assert core.latest_offset() == 2
    assert [m.payload.n for m in core.replay(from_offset=0)] == [1, 2]
    log.close()
