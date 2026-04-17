"""Tests for agentbus.memory — store, pairing, search tool, runtime."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentbus.memory import (
    MEMORY_SEARCH_TOOL,
    MemoryNode,
    MemoryRuntime,
    MemoryStore,
    _cosine,
    _pack_embedding,
    _unpack_embedding,
    build_embedding_provider,
    format_search_results,
    load_memory_config_from_dict,
)

# ── Embedding packing ────────────────────────────────────────────────────────


class TestPacking:
    def test_round_trip(self):
        vec = [0.1, -0.5, 1.0, 0.0, 3.14]
        packed = _pack_embedding(vec)
        unpacked = _unpack_embedding(packed)
        for a, b in zip(vec, unpacked, strict=True):
            assert abs(a - b) < 1e-5

    def test_empty(self):
        assert _unpack_embedding(_pack_embedding([])) == []


class TestCosine:
    def test_identical_is_one(self):
        v = [1.0, 2.0, 3.0]
        assert abs(_cosine(v, v) - 1.0) < 1e-6

    def test_orthogonal_is_zero(self):
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_zero_vector_is_zero(self):
        assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# ── MemoryStore ──────────────────────────────────────────────────────────────


class TestMemoryStore:
    def test_add_and_count(self, tmp_path):
        store = MemoryStore(tmp_path / "m.db")
        try:
            store.add(
                session_id="s1",
                user_text="hello",
                assistant_text="hi",
                embedding=[0.1, 0.2, 0.3],
            )
            assert store.count() == 1
        finally:
            store.close()

    def test_all_turns_returns_records(self, tmp_path):
        store = MemoryStore(tmp_path / "m.db")
        try:
            store.add(
                session_id="s1",
                user_text="q",
                assistant_text="a",
                embedding=[0.5, 0.5],
            )
            turns = store.all_turns()
            assert len(turns) == 1
            assert turns[0].user_text == "q"
            assert turns[0].assistant_text == "a"
            assert turns[0].embedding == pytest.approx([0.5, 0.5])
        finally:
            store.close()

    def test_search_orders_by_similarity(self, tmp_path):
        store = MemoryStore(tmp_path / "m.db")
        try:
            store.add(session_id="s", user_text="cats", assistant_text="x", embedding=[1.0, 0.0])
            store.add(session_id="s", user_text="dogs", assistant_text="y", embedding=[0.0, 1.0])
            store.add(
                session_id="s", user_text="cat-ish", assistant_text="z", embedding=[0.9, 0.1]
            )

            results = store.search([1.0, 0.0], k=2)
            assert len(results) == 2
            top_texts = [r[0].user_text for r in results]
            assert top_texts[0] == "cats"
            assert top_texts[1] == "cat-ish"
        finally:
            store.close()

    def test_search_respects_k(self, tmp_path):
        store = MemoryStore(tmp_path / "m.db")
        try:
            for i in range(10):
                store.add(
                    session_id="s",
                    user_text=f"q{i}",
                    assistant_text="a",
                    embedding=[float(i), 0.0],
                )
            results = store.search([5.0, 0.0], k=3)
            assert len(results) == 3
        finally:
            store.close()

    def test_persistence_across_instances(self, tmp_path):
        path = tmp_path / "m.db"
        s1 = MemoryStore(path)
        try:
            s1.add(session_id="s", user_text="q", assistant_text="a", embedding=[1.0, 2.0])
        finally:
            s1.close()
        s2 = MemoryStore(path)
        try:
            assert s2.count() == 1
        finally:
            s2.close()


# ── Config loading ───────────────────────────────────────────────────────────


class TestLoadMemoryConfig:
    def test_missing_returns_disabled(self):
        assert load_memory_config_from_dict(None) == {"enabled": False}

    def test_false_returns_disabled(self):
        assert load_memory_config_from_dict(False) == {"enabled": False}

    def test_true_returns_ollama_defaults(self):
        cfg = load_memory_config_from_dict(True)
        assert cfg["enabled"] is True
        assert cfg["provider"] == "ollama"
        assert "db_path" in cfg

    def test_dict_overrides_apply(self):
        cfg = load_memory_config_from_dict(
            {"enabled": True, "model": "custom-model", "base_url": "http://x:1"}
        )
        assert cfg["model"] == "custom-model"
        assert cfg["base_url"] == "http://x:1"

    def test_rejects_non_mapping(self):
        with pytest.raises(ValueError):
            load_memory_config_from_dict("not-a-config")


class TestBuildEmbeddingProvider:
    def test_ollama(self):
        provider = build_embedding_provider(
            {"provider": "ollama", "model": "m", "base_url": "http://x:1"}
        )
        assert provider.model == "m"
        assert provider.base_url == "http://x:1"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="unknown embedding provider"):
            build_embedding_provider({"provider": "nonsense"})


# ── format_search_results ────────────────────────────────────────────────────


@dataclass
class _FakeTurn:
    id: int
    session_id: str
    ts: float
    user_text: str
    assistant_text: str
    embedding: list[float]


class TestFormatSearchResults:
    def test_empty_message(self):
        assert "No similar past turns" in format_search_results([])

    def test_includes_preview(self):
        turn = _FakeTurn(
            id=1, session_id="s", ts=0.0, user_text="hi there", assistant_text="hello", embedding=[]
        )
        out = format_search_results([(turn, 0.95)])
        assert "hi there" in out
        assert "hello" in out
        assert "sim=0.95" in out

    def test_truncates_long_texts(self):
        turn = _FakeTurn(
            id=1,
            session_id="s",
            ts=0.0,
            user_text="q" * 500,
            assistant_text="a" * 1000,
            embedding=[],
        )
        out = format_search_results([(turn, 0.5)])
        assert "..." in out


# ── MemoryNode: fakes ────────────────────────────────────────────────────────


class _FakeEmbeddings:
    """Deterministic fake: embedding = [len(text), first-char-ord, 0]."""

    def __init__(self, fail_on: str | None = None) -> None:
        self._fail_on = fail_on
        self.calls: list[str] = []

    @property
    def dim(self) -> int:
        return 3

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        if self._fail_on and any(self._fail_on in t for t in texts):
            raise RuntimeError(f"embed blew up on {self._fail_on!r}")
        return [[float(len(t)), float(ord(t[0]) if t else 0), 0.0] for t in texts]


def _make_runtime(tmp_path, *, fail_on: str | None = None) -> MemoryRuntime:
    return MemoryRuntime(
        store=MemoryStore(tmp_path / "m.db"),
        embeddings=_FakeEmbeddings(fail_on=fail_on),
        session_id="test-session",
    )


# ── MemoryNode: pairing & storage ────────────────────────────────────────────


class TestMemoryNodePairing:
    async def test_inbound_outbound_pair_is_stored(self, tmp_path):
        from agentbus.bus import MessageBus
        from agentbus.schemas.common import InboundChat, OutboundChat
        from agentbus.topic import Topic

        runtime = _make_runtime(tmp_path)
        try:
            bus = MessageBus(socket_path=None)
            bus.register_topic(Topic[InboundChat]("/inbound", retention=10))
            bus.register_topic(Topic[OutboundChat]("/outbound", retention=10))
            from agentbus.schemas.common import ToolRequest
            from agentbus.schemas.common import ToolResult as BusToolResult

            bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
            bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

            bus.register_node(MemoryNode(runtime))

            bus.publish("/inbound", InboundChat(channel="cli", sender="user", text="what's up"))
            bus.publish("/outbound", OutboundChat(text="not much", reply_to="user"))

            await bus.spin(until=lambda: runtime.store.count() >= 1, timeout=5.0)

            assert runtime.store.count() == 1
            turn = runtime.store.all_turns()[0]
            assert turn.user_text == "what's up"
            assert turn.assistant_text == "not much"
            assert turn.session_id == "test-session"
        finally:
            runtime.close()

    async def test_outbound_without_inbound_is_ignored(self, tmp_path):
        from agentbus.bus import MessageBus
        from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        runtime = _make_runtime(tmp_path)
        try:
            bus = MessageBus(socket_path=None)
            bus.register_topic(Topic[InboundChat]("/inbound", retention=10))
            bus.register_topic(Topic[OutboundChat]("/outbound", retention=10))
            bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
            bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

            bus.register_node(MemoryNode(runtime))

            bus.publish("/outbound", OutboundChat(text="orphan", reply_to=None))
            await bus.spin(timeout=0.3)

            assert runtime.store.count() == 0
        finally:
            runtime.close()

    async def test_embedding_failure_drops_turn(self, tmp_path):
        from agentbus.bus import MessageBus
        from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        runtime = _make_runtime(tmp_path, fail_on="boom")
        try:
            bus = MessageBus(socket_path=None)
            bus.register_topic(Topic[InboundChat]("/inbound", retention=10))
            bus.register_topic(Topic[OutboundChat]("/outbound", retention=10))
            bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
            bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

            bus.register_node(MemoryNode(runtime))

            bus.publish("/inbound", InboundChat(channel="cli", sender="u", text="a boom message"))
            bus.publish("/outbound", OutboundChat(text="reply", reply_to="u"))

            # Can't wait for store to fill — it won't. Spin for a short window.
            await bus.spin(timeout=0.3)
            assert runtime.store.count() == 0
        finally:
            runtime.close()


# ── MemoryNode: memory_search tool ───────────────────────────────────────────


class TestMemoryNodeSearch:
    async def test_search_returns_matches(self, tmp_path):
        from agentbus.bus import MessageBus
        from agentbus.message import Message
        from agentbus.node import Node
        from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        runtime = _make_runtime(tmp_path)
        runtime.store.add(
            session_id="s", user_text="dogs are great", assistant_text="yes", embedding=[14.0, 100.0, 0.0]
        )
        runtime.store.add(
            session_id="s", user_text="cats rule", assistant_text="mew", embedding=[9.0, 99.0, 0.0]
        )
        try:
            bus = MessageBus(socket_path=None)
            bus.register_topic(Topic[InboundChat]("/inbound", retention=5))
            bus.register_topic(Topic[OutboundChat]("/outbound", retention=5))
            bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
            bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

            bus.register_node(MemoryNode(runtime))

            captured: list[Message] = []

            class Collector(Node):
                name = "collector"
                subscriptions = ["/tools/result"]
                publications: list[str] = []

                async def on_message(self, msg: Message) -> None:
                    captured.append(msg)

            bus.register_node(Collector())
            bus.publish(
                "/tools/request",
                ToolRequest(tool=MEMORY_SEARCH_TOOL, params={"query": "dogs", "k": 2}),
            )
            await bus.spin(until=lambda: bool(captured), timeout=5.0)

            assert captured
            payload: BusToolResult = captured[0].payload
            assert payload.error is None
            assert payload.output is not None
            assert "dogs" in payload.output or "cats" in payload.output
        finally:
            runtime.close()

    async def test_search_silent_drop_for_unknown_tool(self, tmp_path):
        from agentbus.bus import MessageBus
        from agentbus.message import Message
        from agentbus.node import Node
        from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        runtime = _make_runtime(tmp_path)
        try:
            bus = MessageBus(socket_path=None)
            bus.register_topic(Topic[InboundChat]("/inbound", retention=5))
            bus.register_topic(Topic[OutboundChat]("/outbound", retention=5))
            bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
            bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

            bus.register_node(MemoryNode(runtime))

            captured: list[Message] = []

            class Collector(Node):
                name = "collector"
                subscriptions = ["/tools/result"]
                publications: list[str] = []

                async def on_message(self, msg: Message) -> None:
                    captured.append(msg)

            bus.register_node(Collector())
            bus.publish(
                "/tools/request", ToolRequest(tool="bash", params={"command": "ls"})
            )
            await bus.spin(timeout=0.3)
            assert captured == []
        finally:
            runtime.close()

    async def test_search_empty_query_returns_error(self, tmp_path):
        from agentbus.bus import MessageBus
        from agentbus.message import Message
        from agentbus.node import Node
        from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest
        from agentbus.schemas.common import ToolResult as BusToolResult
        from agentbus.topic import Topic

        runtime = _make_runtime(tmp_path)
        try:
            bus = MessageBus(socket_path=None)
            bus.register_topic(Topic[InboundChat]("/inbound", retention=5))
            bus.register_topic(Topic[OutboundChat]("/outbound", retention=5))
            bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
            bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

            bus.register_node(MemoryNode(runtime))

            captured: list[Message] = []

            class Collector(Node):
                name = "collector"
                subscriptions = ["/tools/result"]
                publications: list[str] = []

                async def on_message(self, msg: Message) -> None:
                    captured.append(msg)

            bus.register_node(Collector())
            bus.publish(
                "/tools/request",
                ToolRequest(tool=MEMORY_SEARCH_TOOL, params={"query": "   "}),
            )
            await bus.spin(until=lambda: bool(captured), timeout=5.0)

            assert captured
            payload: BusToolResult = captured[0].payload
            assert payload.error is not None
            assert "query" in payload.error
        finally:
            runtime.close()


# ── ChatConfig integration ───────────────────────────────────────────────────


class TestChatConfigMemory:
    def test_memory_false_yields_disabled_settings(self):
        from agentbus.chat._config import ChatConfig

        cfg = ChatConfig()
        assert cfg.memory is False
        assert cfg.memory_settings == {"enabled": False}

    def test_memory_true_loaded_from_yaml(self, tmp_path):
        import yaml

        from agentbus.chat._config import load_config

        path = tmp_path / "agentbus.yaml"
        path.write_text(yaml.dump({"provider": "anthropic", "memory": True}), encoding="utf-8")
        cfg = load_config(path)
        assert cfg.memory is True
        assert cfg.memory_settings["enabled"] is True
        assert cfg.memory_settings["provider"] == "ollama"

    def test_memory_mapping_loaded_from_yaml(self, tmp_path):
        import yaml

        from agentbus.chat._config import load_config

        path = tmp_path / "agentbus.yaml"
        path.write_text(
            yaml.dump(
                {
                    "provider": "anthropic",
                    "memory": {
                        "enabled": True,
                        "model": "custom-embed",
                        "db_path": "/tmp/custom.db",
                    },
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.memory is True
        assert cfg.memory_settings["model"] == "custom-embed"
        assert cfg.memory_settings["db_path"] == "/tmp/custom.db"
