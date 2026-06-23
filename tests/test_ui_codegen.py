"""Tests for the UI code generation + workflow design model."""

import sys

import pytest

from agentbus.launch import build_bus_from_config
from agentbus.ui.codegen import (
    CodegenError,
    CodegenWorkspace,
    NodeSpec,
    SchemaSpec,
    generate_node_source,
    generate_schema_source,
    slugify,
)
from agentbus.ui.workflow import WorkflowManager


def test_slugify():
    assert slugify("MyEventNode") == "my_event_node"
    assert slugify("HTTPServer") == "http_server"
    assert slugify("foo bar-baz") == "foo_bar_baz"


def test_generate_schema_source_compiles():
    src = generate_schema_source(
        SchemaSpec(
            class_name="OrderEvent",
            fields=[
                {"name": "order_id", "type": "str"},
                {"name": "qty", "type": "int", "required": False, "default": 1},
            ],
            description="An order.",
        )
    )
    assert "class OrderEvent(BaseModel):" in src
    assert "order_id: str" in src
    assert "qty: int = 1" in src
    compile(src, "<schema>", "exec")  # must be valid Python


def test_generate_node_source_compiles():
    src = generate_node_source(
        NodeSpec(
            class_name="LoggerNode",
            node_name="logger",
            subscriptions=["/inbound"],
            publications=[],
            hooks={"on_message": "self.logger.info('got %s', msg.topic)"},
        )
    )
    assert "class LoggerNode(Node):" in src
    assert "name = 'logger'" in src
    assert "async def on_message(self, msg: Message)" in src
    compile(src, "<node>", "exec")


def test_invalid_class_name_rejected():
    with pytest.raises(CodegenError):
        generate_schema_source(SchemaSpec(class_name="not valid", fields=[]))


def test_workspace_writes_and_imports(tmp_path):
    sys.path.insert(0, str(tmp_path))
    try:
        ws = CodegenWorkspace(tmp_path, package="wf_pkg_a")
        schema_path = ws.write_schema(
            SchemaSpec(class_name="Ping", fields=[{"name": "msg", "type": "str"}])
        )
        assert schema_path == "wf_pkg_a.schemas.ping:Ping"
        model = ws.validate_import(schema_path)
        assert model.__name__ == "Ping"

        node_path = ws.write_node(
            NodeSpec(
                class_name="PingNode",
                node_name="pinger",
                subscriptions=["/ping"],
                hooks={"on_message": "pass"},
            )
        )
        assert node_path == "wf_pkg_a.nodes.ping_node:PingNode"
        ws.validate_import(node_path)
    finally:
        sys.path.remove(str(tmp_path))


def test_workspace_bad_body_raises(tmp_path):
    sys.path.insert(0, str(tmp_path))
    try:
        ws = CodegenWorkspace(tmp_path, package="wf_pkg_b")
        with pytest.raises(CodegenError):
            ws.write_node(
                NodeSpec(
                    class_name="BrokenNode",
                    node_name="broken",
                    hooks={"on_message": "this is not python !!!"},
                )
            )
    finally:
        sys.path.remove(str(tmp_path))


def test_manager_creates_workflow_that_builds(tmp_path):
    """End-to-end: design a topic+node, then build_bus_from_config must load it."""
    sys.path.insert(0, str(tmp_path))
    try:
        config_path = tmp_path / "agentbus.yaml"
        mgr = WorkflowManager(config_path, tmp_path, package="wf_pkg_c")

        mgr.create_topic(
            "/orders",
            schema_class="OrderEvent",
            fields=[{"name": "order_id", "type": "str"}],
            retention=10,
        )
        mgr.create_node(
            "OrderLogger",
            "order_logger",
            subscriptions=["/orders"],
            on_message="self.logger.info('order %s', msg.payload.order_id)",
        )

        config = mgr.read_config()
        assert config["topics"][0]["name"] == "/orders"
        assert config["topics"][0]["schema"] == "wf_pkg_c.schemas.order_event:OrderEvent"
        assert config["nodes"][0]["class"] == "wf_pkg_c.nodes.order_logger:OrderLogger"

        # The generated workflow must produce a real, spinnable bus.
        bus = build_bus_from_config(config)
        assert "/orders" in {t.name for t in bus.topics()}
        assert "order_logger" in {n.name for n in bus.nodes()}
    finally:
        sys.path.remove(str(tmp_path))


def test_manager_set_hook_regenerates(tmp_path):
    sys.path.insert(0, str(tmp_path))
    try:
        mgr = WorkflowManager(tmp_path / "agentbus.yaml", tmp_path, package="wf_pkg_d")
        mgr.create_node("Echo", "echo", subscriptions=["/in"], on_message="pass")
        mgr.set_hook("Echo", "on_message", "self.logger.info('hi')")
        src = (tmp_path / "wf_pkg_d" / "nodes" / "echo.py").read_text()
        assert "self.logger.info('hi')" in src
    finally:
        sys.path.remove(str(tmp_path))
