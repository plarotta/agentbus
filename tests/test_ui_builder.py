"""Tests for the workflow-builder helper agent's tool dispatch.

The LLM/provider is external; these exercise the tool handlers that mutate the
workflow (the part we own), with no network calls.
"""

import sys

from agentbus.launch import build_bus_from_config
from agentbus.schemas.harness import ToolCall
from agentbus.ui.builder import WorkflowBuilder
from agentbus.ui.workflow import WorkflowManager


class _FakeSupervisor:
    def __init__(self):
        self.bus = None
        self.reloaded = 0

    async def reload(self):
        self.reloaded += 1


def _builder(tmp_path, package):
    mgr = WorkflowManager(tmp_path / "agentbus.yaml", tmp_path, package=package)
    sup = _FakeSupervisor()
    # harness sentinel — these tests never call .chat(), only _dispatch/_execute.
    builder = WorkflowBuilder(mgr, sup, harness=object())
    return builder, mgr, sup


async def test_builder_creates_topic_and_node(tmp_path):
    sys.path.insert(0, str(tmp_path))
    try:
        builder, mgr, _ = _builder(tmp_path, "bld_pkg_a")

        out = await builder._dispatch(
            "create_topic",
            {
                "name": "/orders",
                "schema_class": "OrderEvent",
                "fields": [{"name": "order_id", "type": "str"}],
            },
        )
        assert "/orders" in out

        out = await builder._dispatch(
            "create_node",
            {
                "class_name": "OrderLogger",
                "node_name": "order_logger",
                "subscriptions": ["/orders"],
                "on_message": "self.logger.info('order')",
            },
        )
        assert "order_logger" in out

        # The designed workflow must build into a real bus.
        bus = build_bus_from_config(mgr.read_config())
        assert "/orders" in {t.name for t in bus.topics()}
        assert "order_logger" in {n.name for n in bus.nodes()}
    finally:
        sys.path.remove(str(tmp_path))


async def test_builder_set_hook_and_apply(tmp_path):
    sys.path.insert(0, str(tmp_path))
    try:
        builder, mgr, sup = _builder(tmp_path, "bld_pkg_b")
        await builder._dispatch(
            "create_node",
            {"class_name": "Echo", "node_name": "echo", "on_message": "pass"},
        )
        await builder._dispatch(
            "set_hook",
            {"class_name": "Echo", "hook": "on_message", "body": "self.logger.info('hi')"},
        )
        src = (tmp_path / "bld_pkg_b" / "nodes" / "echo.py").read_text()
        assert "self.logger.info('hi')" in src

        await builder._dispatch("apply_changes", {})
        assert sup.reloaded == 1
    finally:
        sys.path.remove(str(tmp_path))


async def test_builder_execute_reports_errors(tmp_path):
    sys.path.insert(0, str(tmp_path))
    try:
        builder, _, _ = _builder(tmp_path, "bld_pkg_c")
        # Missing required args → handler raises → reported as ToolResult.error.
        result = await builder._execute(ToolCall(id="1", name="create_topic", arguments={}))
        assert result.error is not None
        assert result.output is None
    finally:
        sys.path.remove(str(tmp_path))


async def test_builder_read_workflow(tmp_path):
    sys.path.insert(0, str(tmp_path))
    try:
        builder, mgr, _ = _builder(tmp_path, "bld_pkg_d")
        await builder._dispatch(
            "create_topic",
            {"name": "/x", "schema_class": "X", "fields": [{"name": "a", "type": "int"}]},
        )
        out = await builder._dispatch("read_workflow", {})
        assert "/x" in out
    finally:
        sys.path.remove(str(tmp_path))
