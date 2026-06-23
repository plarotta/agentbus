"""AgentBus web UI — workflow designer, introspection, and replay.

Public surface is intentionally small. ``serve`` builds a bus from a config and
runs the FastAPI app; ``build_app`` / ``BusSupervisor`` are exposed for tests and
embedders. Heavy deps (FastAPI/uvicorn) are imported lazily inside ``server``.
"""

from agentbus.ui.builder import WorkflowBuilder
from agentbus.ui.server import BusSupervisor, build_app, serve
from agentbus.ui.workflow import WorkflowManager

__all__ = ["BusSupervisor", "WorkflowBuilder", "WorkflowManager", "build_app", "serve"]
