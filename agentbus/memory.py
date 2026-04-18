"""Background memory consolidation for AgentBus chat sessions.

Every conversation turn (a ``/inbound`` user message paired with its
``/outbound`` assistant reply) is embedded and written to a local
SQLite store. A ``memory_search`` tool is exposed to the planner so
the LLM can recall prior turns by semantic similarity.

Storage lives at ``~/.agentbus/memory.db`` by default. Embeddings are
stored as raw float32 blobs; search is a Python-level cosine scan
over all rows. At conversation-turn scale (hundreds to low thousands
of rows) that's sub-millisecond. When this becomes a scale problem,
swap in ``sqlite-vec`` or Chroma behind the :class:`MemoryStore`
interface.

Pairing strategy: MemoryNode subscribes to both ``/inbound`` and
``/outbound`` and keeps the most recent inbound per channel. When the
next outbound arrives it pairs them and commits a turn. This is
correct for single-user chat sessions. If we ever need multi-user
pairing, the planner will need to propagate ``correlation_id`` from
inbound to outbound — that change is additive.

Embeddings are pluggable via the :class:`EmbeddingProvider` protocol.
The default adapter calls Ollama's ``/api/embed`` endpoint over HTTP
using the existing ``httpx`` extra. If the provider is unreachable at
startup, :func:`open_memory_runtime` raises; if a single turn fails
to embed mid-session, MemoryNode logs the failure and drops the turn
rather than crashing the conversation.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentbus.harness.providers import ToolSchema
from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest
from agentbus.schemas.common import ToolResult as BusToolResult

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".agentbus" / "memory.db"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text"

MEMORY_SEARCH_TOOL = "memory_search"


# ── Embedding providers ──────────────────────────────────────────────────────


class EmbeddingProvider(Protocol):
    """Produces fixed-dimension float vectors for a batch of texts."""

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class OllamaEmbeddings:
    """Ollama ``/api/embed`` client.

    Requires the ``httpx`` extra (already used by the Ollama LLM
    provider). Uses ``/api/embed`` (the batched endpoint), falling
    back to ``/api/embeddings`` per-text if the server is old.
    """

    model: str = DEFAULT_EMBED_MODEL
    base_url: str = DEFAULT_OLLAMA_URL
    _dim: int | None = field(default=None, init=False, repr=False)

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise RuntimeError("Embedding dim unknown — call embed() at least once first")
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            import httpx
        except ModuleNotFoundError:
            raise SystemExit(
                "Error: the 'httpx' package is required for MemoryNode with ollama embeddings.\n"
                "Install it with:  uv sync --extra ollama"
            ) from None

        if not texts:
            return []

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
            vectors = data.get("embeddings")
            if not vectors:
                raise RuntimeError(f"ollama /api/embed returned no embeddings: {data}")
            if self._dim is None:
                self._dim = len(vectors[0])
            return [[float(x) for x in v] for v in vectors]


# ── Storage ──────────────────────────────────────────────────────────────────


@dataclass
class TurnRecord:
    id: int
    session_id: str
    ts: float
    user_text: str
    assistant_text: str
    embedding: list[float]


def _pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length float vectors."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0.0:
        return 0.0
    return dot / denom


class MemoryStore:
    """SQLite-backed turn store with cosine similarity search.

    Not thread-safe; single-process use only. All methods are
    synchronous and fast enough to call from within an async handler.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                ts REAL NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                embedding BLOB NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add(
        self,
        *,
        session_id: str,
        user_text: str,
        assistant_text: str,
        embedding: list[float],
        ts: float | None = None,
    ) -> int:
        now = ts if ts is not None else time.time()
        cur = self._conn.execute(
            "INSERT INTO turns(session_id, ts, user_text, assistant_text, embedding) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, now, user_text, assistant_text, _pack_embedding(embedding)),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        assert row_id is not None
        return row_id

    def all_turns(self) -> list[TurnRecord]:
        rows = self._conn.execute(
            "SELECT id, session_id, ts, user_text, assistant_text, embedding FROM turns"
        ).fetchall()
        return [
            TurnRecord(
                id=r[0],
                session_id=r[1],
                ts=r[2],
                user_text=r[3],
                assistant_text=r[4],
                embedding=_unpack_embedding(r[5]),
            )
            for r in rows
        ]

    def search(self, query_embedding: list[float], *, k: int = 5) -> list[tuple[TurnRecord, float]]:
        """Return top-k (turn, cosine) pairs, highest-similarity first."""
        turns = self.all_turns()
        scored = [(t, _cosine(query_embedding, t.embedding)) for t in turns]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM turns").fetchone()
        return int(row[0])


# ── Runtime ──────────────────────────────────────────────────────────────────


@dataclass
class MemoryRuntime:
    store: MemoryStore
    embeddings: EmbeddingProvider
    session_id: str

    def close(self) -> None:
        self.store.close()


async def open_memory_runtime(
    *,
    session_id: str,
    db_path: Path = DEFAULT_DB_PATH,
    embeddings: EmbeddingProvider | None = None,
) -> MemoryRuntime:
    """Open the SQLite store and probe the embedding provider.

    Probing runs one embedding of an empty-ish sentence so failures
    surface at startup rather than on the first turn. If the provider
    is unreachable this raises — the chat runner catches the exception
    and falls back to no-memory mode rather than aborting the session.
    """
    provider = embeddings if embeddings is not None else OllamaEmbeddings()
    # Probe the provider so Ollama-not-running / model-not-pulled is caught here.
    await provider.embed(["hello"])
    store = MemoryStore(db_path=db_path)
    return MemoryRuntime(store=store, embeddings=provider, session_id=session_id)


# ── Tool schema ──────────────────────────────────────────────────────────────


MEMORY_SEARCH_SCHEMA = ToolSchema(
    name=MEMORY_SEARCH_TOOL,
    description=(
        "Search your long-term memory of prior conversation turns by semantic "
        "similarity. Use this when the user references something from an "
        "earlier session, or when recalling context would help."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language query to match against past turns",
            },
            "k": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 5)",
            },
        },
        "required": ["query"],
    },
)


def format_search_results(results: list[tuple[TurnRecord, float]]) -> str:
    if not results:
        return "No similar past turns found."
    lines = [f"[{len(results)} similar past turns]"]
    for i, (turn, score) in enumerate(results, 1):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(turn.ts))
        user_preview = turn.user_text.strip().replace("\n", " ")
        assistant_preview = turn.assistant_text.strip().replace("\n", " ")
        if len(user_preview) > 200:
            user_preview = user_preview[:197] + "..."
        if len(assistant_preview) > 400:
            assistant_preview = assistant_preview[:397] + "..."
        lines.append(
            f"{i}. ({when}, sim={score:.2f})\n"
            f"   User: {user_preview}\n"
            f"   Assistant: {assistant_preview}"
        )
    return "\n".join(lines)


# ── MemoryNode ───────────────────────────────────────────────────────────────


class MemoryNode(Node):
    """Consolidates conversation turns and serves ``memory_search``.

    Subscribes to ``/inbound`` to track the latest user message per
    channel; on ``/outbound`` arrival, pairs with the stored inbound,
    embeds the combined turn text, and writes to the store. Also
    subscribes to ``/tools/request`` and responds only to calls for
    the ``memory_search`` tool — unknown tools are silently dropped so
    ChatToolNode / MCPGatewayNode can handle them.

    Embedding failures on a single turn are logged and the turn is
    skipped. Embedding failures during ``memory_search`` return an
    error result so the LLM sees it as a tool failure rather than
    getting stuck.
    """

    name = "memory"
    subscriptions = ["/inbound", "/outbound", "/tools/request"]
    publications = ["/tools/result"]
    concurrency_mode = "serial"  # one-at-a-time writes to sqlite

    def __init__(self, runtime: MemoryRuntime) -> None:
        self._runtime = runtime
        self._bus: Any = None
        self._pending_inbound: dict[str, InboundChat] = {}

    async def on_init(self, bus: Any) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        topic = msg.topic
        if topic == "/inbound":
            inbound: InboundChat = msg.payload
            self._pending_inbound[inbound.channel] = inbound
            return
        if topic == "/outbound":
            await self._handle_outbound(msg.payload)
            return
        if topic == "/tools/request":
            await self._handle_tool_request(msg)
            return

    async def _handle_outbound(self, outbound: OutboundChat) -> None:
        # Pair with the most recent pending inbound. In single-user chat
        # there's exactly one channel active, so this is unambiguous.
        if not self._pending_inbound:
            return
        # Pop any pending inbound — multi-channel pairing would need
        # channel routing, but for now we take whichever came in.
        _channel, inbound = self._pending_inbound.popitem()
        combined = f"User: {inbound.text}\nAssistant: {outbound.text}"
        try:
            vectors = await self._runtime.embeddings.embed([combined])
        except Exception:
            logger.warning("memory: embedding failed, dropping turn", exc_info=True)
            return
        if not vectors:
            return
        try:
            self._runtime.store.add(
                session_id=self._runtime.session_id,
                user_text=inbound.text,
                assistant_text=outbound.text,
                embedding=vectors[0],
            )
        except Exception:
            logger.exception("memory: failed to persist turn")

    async def _handle_tool_request(self, msg: Message) -> None:
        request: ToolRequest = msg.payload
        if request.tool != MEMORY_SEARCH_TOOL:
            return

        query = str(request.params.get("query", "")).strip()
        if not query:
            await self._publish_result(msg, output=None, error="memory_search requires 'query'")
            return
        try:
            k = int(request.params.get("k", 5))
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, 25))

        try:
            vectors = await self._runtime.embeddings.embed([query])
        except Exception as exc:
            await self._publish_result(msg, output=None, error=f"embed failed: {exc}")
            return
        if not vectors:
            await self._publish_result(msg, output=None, error="embed returned empty")
            return

        results = self._runtime.store.search(vectors[0], k=k)
        await self._publish_result(msg, output=format_search_results(results), error=None)

    async def _publish_result(self, msg: Message, *, output: str | None, error: str | None) -> None:
        await self._bus.publish(
            "/tools/result",
            BusToolResult(tool_call_id=msg.id, output=output, error=error),
            correlation_id=msg.correlation_id,
        )


# ── Config loading ───────────────────────────────────────────────────────────


def load_memory_config_from_dict(data: Any) -> dict[str, Any]:
    """Normalize the ``memory:`` key in agentbus.yaml into a config dict.

    Accepts either ``memory: true`` / ``memory: false`` (bool shorthand)
    or a mapping with keys ``enabled``, ``provider``, ``model``,
    ``base_url``, ``db_path``. Returns ``{"enabled": False}`` when
    disabled.
    """
    if data is None or data is False:
        return {"enabled": False}
    if data is True:
        return {
            "enabled": True,
            "provider": "ollama",
            "model": DEFAULT_EMBED_MODEL,
            "base_url": DEFAULT_OLLAMA_URL,
            "db_path": str(DEFAULT_DB_PATH),
        }
    if not isinstance(data, dict):
        raise ValueError(f"memory must be bool or mapping, got {type(data).__name__}")
    return {
        "enabled": bool(data.get("enabled", True)),
        "provider": data.get("provider", "ollama"),
        "model": data.get("model", DEFAULT_EMBED_MODEL),
        "base_url": data.get("base_url", DEFAULT_OLLAMA_URL),
        "db_path": data.get("db_path", str(DEFAULT_DB_PATH)),
    }


def build_embedding_provider(config: dict[str, Any]) -> EmbeddingProvider:
    """Construct an EmbeddingProvider from a normalized memory config."""
    provider = config.get("provider", "ollama")
    if provider == "ollama":
        return OllamaEmbeddings(
            model=config.get("model", DEFAULT_EMBED_MODEL),
            base_url=config.get("base_url", DEFAULT_OLLAMA_URL),
        )
    raise ValueError(f"unknown embedding provider: {provider!r}")


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_EMBED_MODEL",
    "MEMORY_SEARCH_SCHEMA",
    "MEMORY_SEARCH_TOOL",
    "EmbeddingProvider",
    "MemoryNode",
    "MemoryRuntime",
    "MemoryStore",
    "OllamaEmbeddings",
    "TurnRecord",
    "build_embedding_provider",
    "format_search_results",
    "load_memory_config_from_dict",
    "open_memory_runtime",
]
