"""Tests for the UI FastAPI server (introspection, config CRUD, WS replay)."""

import sys

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agentbus.ui.server import BusSupervisor, build_app  # noqa: E402
from agentbus.ui.workflow import WorkflowManager  # noqa: E402

_BASE_CONFIG = """
bus:
  introspection_socket: null
topics:
  - name: /inbound
    schema: agentbus.schemas.common:InboundChat
    retention: 50
"""


def _make_client(tmp_path, config_text=_BASE_CONFIG):
    config_path = tmp_path / "agentbus.yaml"
    config_path.write_text(config_text)
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    supervisor = BusSupervisor(config_path)
    manager = WorkflowManager(config_path, tmp_path, package="srv_pkg")
    app = build_app(supervisor, manager)
    return TestClient(app), supervisor


def test_health_and_introspection(tmp_path):
    client, _ = _make_client(tmp_path)
    with client:
        assert client.get("/api/health").json()["running"] is True

        topics = client.get("/api/topics").json()
        assert "/inbound" in {t["name"] for t in topics}

        graph = client.get("/api/graph").json()
        assert "nodes" in graph and "topics" in graph and "edges" in graph

        assert client.get("/api/nodes").json() == []
        assert client.get("/api/latest-offset").json()["offset"] == 0


def test_config_crud(tmp_path):
    client, _ = _make_client(tmp_path)
    with client:
        cfg = client.get("/api/config").json()
        assert cfg["topics"][0]["name"] == "/inbound"

        cfg["topics"].append({"name": "/extra", "schema": "agentbus.schemas.common:InboundChat"})
        assert client.put("/api/config", json=cfg).status_code == 200
        assert any(t["name"] == "/extra" for t in client.get("/api/config").json()["topics"])


def test_design_then_apply_reflects_in_live_bus(tmp_path):
    client, _ = _make_client(tmp_path)
    with client:
        # New topic with a generated schema.
        r = client.post(
            "/api/topics",
            json={
                "name": "/orders",
                "schema_class": "OrderEvent",
                "fields": [{"name": "order_id", "type": "str"}],
                "retention": 10,
            },
        )
        assert r.status_code == 200, r.text

        # New node that subscribes to it.
        r = client.post(
            "/api/nodes",
            json={
                "class_name": "OrderLogger",
                "node_name": "order_logger",
                "subscriptions": ["/orders"],
                "on_message": "self.logger.info('order')",
            },
        )
        assert r.status_code == 200, r.text

        # Not live until applied.
        assert "/orders" not in {t["name"] for t in client.get("/api/topics").json()}

        assert client.post("/api/apply").status_code == 200
        assert "/orders" in {t["name"] for t in client.get("/api/topics").json()}
        assert "order_logger" in {n["name"] for n in client.get("/api/nodes").json()}


def test_apply_with_broken_node_keeps_old_bus(tmp_path):
    client, _ = _make_client(tmp_path)
    with client:
        # Hand-write a config entry pointing at a non-importable class.
        cfg = client.get("/api/config").json()
        cfg.setdefault("nodes", []).append({"class": "no.such.module:Nope"})
        client.put("/api/config", json=cfg)

        r = client.post("/api/apply")
        assert r.status_code == 400
        # Old bus still answers.
        assert client.get("/api/health").json()["running"] is True


_SEED_CONFIG = """
bus:
  introspection_socket: null
topics:
  - name: /seed
    schema: agentbus.schemas.common:InboundChat
    retention: 50
nodes:
  - class: srv_seed_pkg.nodes.seeder:Seeder
"""


def test_ws_stream_replay(tmp_path):
    """A generated seeder logs 3 messages; a WS resume from offset 0 replays them."""
    # Generate the seeder node before the bus is built.
    mgr = WorkflowManager(tmp_path / "agentbus.yaml", tmp_path, package="srv_seed_pkg")
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    mgr.create_node(
        "Seeder",
        "seeder",
        publications=["/seed"],
        on_init=(
            "from agentbus.schemas.common import InboundChat\n"
            "for i in range(3):\n"
            "    await bus.publish('/seed', "
            "InboundChat(channel='c', sender='s', text=f'm{i}'))"
        ),
    )

    client, _ = _make_client(tmp_path, _SEED_CONFIG)
    with client:
        received = []
        with client.websocket_connect("/ws/stream?pattern=/seed&from_offset=0") as ws:
            for _ in range(3):
                received.append(ws.receive_json())
        texts = [m["payload"]["text"] for m in received]
        assert texts == ["m0", "m1", "m2"]
        assert [m["offset"] for m in received] == [1, 2, 3]
