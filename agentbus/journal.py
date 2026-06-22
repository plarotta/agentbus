"""Durable message journal — a SQLite-backed ``LogStore``.

Swap it into a ``BusCore`` (or ``MessageBus(log_store=...)``) to make the log
survive process restarts, enabling resume-after-kill replay. Structurally
satisfies ``core.LogStore``; needs no extra dependency (stdlib ``sqlite3``).

Offsets are the table's ``AUTOINCREMENT`` primary key: monotonic and never
reused, even across restarts and after ``prune`` — exactly the cursor semantics
replay needs. Mirrors ``memory.py``'s model: one thread-affine connection,
called synchronously from the synchronous ``BusCore.publish`` (so no executor /
thread-pool, which would violate SQLite's thread affinity).

``read`` hydrates each row to a typed ``Message`` against the *current* schema
registry and **skips** (with a warning) any entry whose payload no longer
validates — schema drift never aborts a replay.
"""

import json
import logging
import sqlite3
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from agentbus.contracts import TOPIC_SCHEMAS, Envelope
from agentbus.errors import TopicSchemaError
from agentbus.message import Message
from agentbus.topic import _match_pattern

logger = logging.getLogger(__name__)

_TAIL_MAXLEN_DEFAULT = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    offset         INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT NOT NULL,
    ts             TEXT NOT NULL,
    topic          TEXT NOT NULL,
    source_node    TEXT NOT NULL,
    correlation_id TEXT,
    reply_to       TEXT,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_topic ON journal(topic);
"""


def _payload_json(payload: object) -> str:
    if isinstance(payload, BaseModel):
        return payload.model_dump_json()  # handles nested datetimes/UUIDs
    return json.dumps(payload)


class SqliteLog:
    """Durable ``LogStore`` backed by a SQLite file.

    ``tail`` is a bounded in-memory cache of recently appended messages for fast
    introspection; it starts empty on reopen (durable history still comes back
    through ``read``, which queries SQLite).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        schemas: dict | None = None,
        tail_maxlen: int = _TAIL_MAXLEN_DEFAULT,
    ) -> None:
        self.path = str(path)
        self.schemas = TOPIC_SCHEMAS if schemas is None else schemas
        self.tail: deque[Message] = deque(maxlen=tail_maxlen)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def append(self, msg: Message) -> Message:
        cur = self._conn.execute(
            "INSERT INTO journal(id, ts, topic, source_node, correlation_id, reply_to, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                msg.id,
                msg.timestamp.isoformat(),
                msg.topic,
                msg.source_node,
                msg.correlation_id,
                msg.reply_to,
                _payload_json(msg.payload),
            ),
        )
        self._conn.commit()
        offset = cur.lastrowid
        stamped = msg.model_copy(update={"offset": offset})
        self.tail.append(stamped)
        return stamped

    def read(
        self, *, from_offset: int = 0, topic: str | None = None, limit: int | None = None
    ) -> Iterator[Message]:
        cur = self._conn.execute(
            "SELECT offset, id, ts, topic, source_node, correlation_id, reply_to, payload "
            "FROM journal WHERE offset > ? ORDER BY offset",
            (from_offset,),
        )
        count = 0
        for offset, id_, ts, t, source_node, cid, reply_to, payload_json in cur:
            if topic is not None and not _match_pattern(topic, t):
                continue
            env = Envelope(
                id=id_,
                timestamp=ts,
                source_node=source_node,
                topic=t,
                correlation_id=cid,
                reply_to=reply_to,
                offset=offset,
                payload=json.loads(payload_json),
            )
            try:
                msg = env.to_message(registry=self.schemas)
            except TopicSchemaError as e:
                logger.warning("replay: skipping offset %d on %r (schema drift): %s", offset, t, e)
                continue
            yield msg
            count += 1
            if limit is not None and count >= limit:
                return

    def latest_offset(self) -> int:
        # sqlite_sequence holds the AUTOINCREMENT high-water mark, which survives
        # pruning of old rows — so the cutover offset stays monotonic.
        row = self._conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'journal'"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def prune(self, before_offset: int) -> int:
        cur = self._conn.execute("DELETE FROM journal WHERE offset < ?", (before_offset,))
        self._conn.commit()
        while self.tail and self.tail[0].offset is not None and self.tail[0].offset < before_offset:
            self.tail.popleft()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()
