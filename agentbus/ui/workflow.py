"""Workflow design model — the shared write path for the UI.

Both the REST endpoints (``server.py``) and the helper agent
(``builder_node.py``) mutate a workflow through this one class, so the two can't
drift. A ``WorkflowManager`` owns:

  * ``agentbus.yaml``        — the declarative bus config (topics + nodes)
  * the generated package    — Python for schemas/nodes (via ``CodegenWorkspace``)
  * ``<package>/_manifest.json`` — structured specs for everything generated, so
                                 an edit regenerates the file from the spec
                                 instead of parsing generated Python.

Topics/nodes added here are wired into ``agentbus.yaml`` by import path exactly
as ``build_bus_from_config`` expects, so a reload picks them up unchanged.
"""

import json
from pathlib import Path
from typing import Any

import yaml

from agentbus.harness.session import _atomic_write_text
from agentbus.ui.codegen import CodegenWorkspace, FieldSpec, NodeSpec, SchemaSpec


class WorkflowManager:
    def __init__(
        self,
        config_path: Path | str,
        codegen_root: Path | str,
        *,
        package: str = "agentbus_workflow",
    ) -> None:
        self.config_path = Path(config_path)
        self.workspace = CodegenWorkspace(codegen_root, package=package)
        self._manifest_path = self.workspace.pkg_dir / "_manifest.json"

    # ── config (agentbus.yaml) ────────────────────────────────────────────────

    def read_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def write_config(self, config: dict[str, Any]) -> None:
        _atomic_write_text(
            self.config_path,
            yaml.dump(config, default_flow_style=False, sort_keys=False),
        )

    # ── manifest (structured specs for generated entities) ────────────────────

    def read_manifest(self) -> dict[str, Any]:
        if not self._manifest_path.exists():
            return {"schemas": {}, "nodes": {}}
        data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        data.setdefault("schemas", {})
        data.setdefault("nodes", {})
        return data

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self.workspace._ensure_pkg()
        _atomic_write_text(self._manifest_path, json.dumps(manifest, indent=2))

    # ── create / edit operations ──────────────────────────────────────────────

    def create_schema(
        self, class_name: str, fields: list[dict[str, Any]], description: str = ""
    ) -> str:
        spec = SchemaSpec(
            class_name=class_name,
            fields=[FieldSpec(**f) for f in fields],
            description=description,
        )
        import_path = self.workspace.write_schema(spec)
        manifest = self.read_manifest()
        manifest["schemas"][class_name] = {
            "fields": fields,
            "description": description,
            "import_path": import_path,
        }
        self._write_manifest(manifest)
        return import_path

    def create_topic(
        self,
        name: str,
        *,
        schema: str | None = None,
        schema_class: str | None = None,
        fields: list[dict[str, Any]] | None = None,
        retention: int = 0,
        description: str = "",
    ) -> dict[str, Any]:
        """Add a topic to the config.

        Provide either ``schema`` (an existing ``module:Class`` import path) or
        ``schema_class`` + ``fields`` to codegen a new Pydantic model first.
        """
        if schema is None:
            if not schema_class or fields is None:
                raise ValueError("provide `schema` or (`schema_class` and `fields`)")
            schema = self.create_schema(schema_class, fields, description)

        entry: dict[str, Any] = {"name": name, "schema": schema}
        if retention:
            entry["retention"] = retention
        if description:
            entry["description"] = description

        config = self.read_config()
        topics = config.setdefault("topics", [])
        topics[:] = [t for t in topics if t.get("name") != name]  # upsert by name
        topics.append(entry)
        self.write_config(config)
        return entry

    def create_node(
        self,
        class_name: str,
        node_name: str,
        *,
        subscriptions: list[str] | None = None,
        publications: list[str] | None = None,
        on_message: str = "",
        on_init: str = "",
        on_shutdown: str = "",
        concurrency: int = 1,
        concurrency_mode: str = "parallel",
        description: str = "",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hooks = {"on_message": on_message, "on_init": on_init, "on_shutdown": on_shutdown}
        spec = NodeSpec(
            class_name=class_name,
            node_name=node_name,
            subscriptions=subscriptions or [],
            publications=publications or [],
            concurrency=concurrency,
            concurrency_mode=concurrency_mode,
            hooks=hooks,
            description=description,
        )
        import_path = self.workspace.write_node(spec)

        manifest = self.read_manifest()
        manifest["nodes"][class_name] = {
            "node_name": node_name,
            "subscriptions": subscriptions or [],
            "publications": publications or [],
            "concurrency": concurrency,
            "concurrency_mode": concurrency_mode,
            "hooks": hooks,
            "description": description,
            "import_path": import_path,
        }
        self._write_manifest(manifest)

        entry: dict[str, Any] = {"class": import_path}
        if config:
            entry["config"] = config
        if concurrency != 1:
            entry["concurrency"] = concurrency

        cfg = self.read_config()
        nodes = cfg.setdefault("nodes", [])
        nodes[:] = [n for n in nodes if n.get("class") != import_path]
        nodes.append(entry)
        self.write_config(cfg)
        return entry

    def set_hook(self, class_name: str, hook: str, body: str) -> str:
        """Rewrite one hook body of a previously generated node and regenerate it."""
        if hook not in ("on_init", "on_message", "on_shutdown"):
            raise ValueError(f"unknown hook: {hook!r}")
        manifest = self.read_manifest()
        spec_dict = manifest["nodes"].get(class_name)
        if spec_dict is None:
            raise ValueError(f"unknown generated node: {class_name!r}")
        spec_dict["hooks"][hook] = body
        spec = NodeSpec(
            class_name=class_name,
            node_name=spec_dict["node_name"],
            subscriptions=spec_dict["subscriptions"],
            publications=spec_dict["publications"],
            concurrency=spec_dict["concurrency"],
            concurrency_mode=spec_dict["concurrency_mode"],
            hooks=spec_dict["hooks"],
            description=spec_dict.get("description", ""),
        )
        import_path = self.workspace.write_node(spec)
        self._write_manifest(manifest)
        return import_path


__all__ = ["WorkflowManager"]
