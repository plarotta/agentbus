"""FastAPI web server for the AgentBus UI.

One process owns both a live ``MessageBus`` (built from ``agentbus.yaml`` via
``build_bus_from_config`` and run with ``spin()`` as a background task) and the
HTTP/WS server that introspects it. REST reads come straight off the live bus;
writes mutate the config + generated code through a ``WorkflowManager`` and take
effect on the next ``/api/apply`` (which rebuilds and restarts the bus).

FastAPI/uvicorn are an optional dependency (the ``ui`` extra); they're imported
lazily so ``import agentbus`` works without them and a missing install fails fast
at ``agentbus ui`` startup with a clear hint.

Known v1 limitation: ``/api/apply`` restarts the bus by cancelling the spin task,
so node ``on_shutdown`` hooks are not run on reload — acceptable for a design-time
tool; revisit if a node holds external resources that need graceful release.
"""

import asyncio
import contextlib
import dataclasses
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from agentbus.contracts import Envelope
from agentbus.launch import build_bus_from_config
from agentbus.server import _ConnectionSink
from agentbus.ui.codegen import CodegenError
from agentbus.ui.workflow import WorkflowManager

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


def _require_fastapi() -> tuple[Any, Any]:
    try:
        import fastapi
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised via the CLI
        raise RuntimeError(
            "The web UI needs FastAPI + uvicorn. Install them with:\n"
            "    uv sync --extra ui\n"
            "or: pip install 'agentbus[ui]'"
        ) from exc
    return fastapi, uvicorn


class BusSupervisor:
    """Owns the live bus and its spin task; rebuilds it on demand."""

    def __init__(self, config_path: Path | str, *, disable_socket: bool = True) -> None:
        self.config_path = Path(config_path)
        self.disable_socket = disable_socket
        self.bus: Any = None
        self._spin_task: asyncio.Task | None = None

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def _build(self) -> Any:
        config = self._load_config()
        if self.disable_socket:
            config.setdefault("bus", {})["introspection_socket"] = None
        return build_bus_from_config(config)

    async def start(self) -> None:
        self.bus = self._build()
        self._spin_task = asyncio.create_task(self.bus.spin(drain_timeout=2.0))

    async def _stop_current(self) -> None:
        if self._spin_task is not None:
            self._spin_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._spin_task
            self._spin_task = None

    async def stop(self) -> None:
        await self._stop_current()
        self.bus = None

    async def reload(self) -> None:
        # Build the new bus *before* tearing down the old one so an import error
        # in freshly generated code leaves the running bus untouched.
        new_bus = self._build()
        await self._stop_current()
        self.bus = new_bus
        self._spin_task = asyncio.create_task(new_bus.spin(drain_timeout=2.0))


def build_app(supervisor: BusSupervisor, manager: WorkflowManager) -> Any:
    _require_fastapi()  # fail fast if the `ui` extra isn't installed
    from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
    from fastapi.staticfiles import StaticFiles

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any):
        await supervisor.start()
        try:
            yield
        finally:
            await supervisor.stop()

    app = FastAPI(title="AgentBus UI", lifespan=lifespan)

    def _bus() -> Any:
        if supervisor.bus is None:
            raise HTTPException(status_code=503, detail="bus not running")
        return supervisor.bus

    # ── introspection (live bus) ──────────────────────────────────────────────

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "running": supervisor.bus is not None}

    @app.get("/api/graph")
    def get_graph() -> dict:
        return dataclasses.asdict(_bus().graph())

    @app.get("/api/topics")
    def get_topics() -> list[dict]:
        return [dataclasses.asdict(t) for t in _bus().topics()]

    @app.get("/api/nodes")
    def get_nodes() -> list[dict]:
        return [dataclasses.asdict(n) for n in _bus().nodes()]

    @app.get("/api/history")
    def get_history(topic: str = Query(...), n: int = Query(20)) -> list[dict]:
        return [Envelope.from_message(m).model_dump(mode="json") for m in _bus().history(topic, n)]

    @app.get("/api/latest-offset")
    def latest_offset() -> dict:
        return {"offset": _bus().latest_offset()}

    # ── config / design CRUD ──────────────────────────────────────────────────

    @app.get("/api/config")
    def get_config() -> dict:
        return manager.read_config()

    @app.put("/api/config")
    def put_config(config: dict = Body(...)) -> dict:
        manager.write_config(config)
        return {"ok": True}

    @app.get("/api/manifest")
    def get_manifest() -> dict:
        return manager.read_manifest()

    @app.post("/api/topics")
    def create_topic(spec: dict = Body(...)) -> dict:
        try:
            return manager.create_topic(**spec)
        except (CodegenError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/nodes")
    def create_node(spec: dict = Body(...)) -> dict:
        try:
            return manager.create_node(**spec)
        except (CodegenError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/nodes/{class_name}/hooks")
    def set_hook(class_name: str, body: dict = Body(...)) -> dict:
        try:
            import_path = manager.set_hook(class_name, body["hook"], body.get("body", ""))
        except (CodegenError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "import_path": import_path}

    @app.post("/api/apply")
    async def apply() -> dict:
        try:
            await supervisor.reload()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"reload failed: {exc}") from exc
        return {"ok": True}

    # ── workflow-builder helper agent ─────────────────────────────────────────

    builder_holder: dict[str, Any] = {"instance": None}

    def _get_builder() -> Any:
        if builder_holder["instance"] is None:
            from agentbus.ui.builder import DEFAULT_BUILDER_MODEL, WorkflowBuilder

            cfg = manager.read_config()
            try:
                builder_holder["instance"] = WorkflowBuilder(
                    manager,
                    supervisor,
                    provider_name=cfg.get("provider", "anthropic"),
                    model=cfg.get("model", DEFAULT_BUILDER_MODEL),
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return builder_holder["instance"]

    @app.post("/api/builder")
    async def builder_chat(body: dict = Body(...)) -> dict:
        message = body.get("message", "")
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        builder = _get_builder()
        try:
            response = await builder.chat(message)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"builder error: {exc}") from exc
        return {"response": response}

    # ── live tap + replay ─────────────────────────────────────────────────────

    @app.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket) -> None:
        await ws.accept()
        bus = supervisor.bus
        if bus is None:
            await ws.close(code=1011)
            return

        pattern = ws.query_params.get("pattern", "/**")
        from_offset_raw = ws.query_params.get("from_offset")
        queue: asyncio.Queue[Envelope] = asyncio.Queue()
        sink = _ConnectionSink(queue)
        target = bus.local_target()

        # Atomic attach + replay (no await between snapshot and subscribe), so the
        # replayed backlog and the live stream join gap-free. Mirrors BusServer.
        if from_offset_raw is None:
            unsub = target.subscribe(pattern, sink)
        else:
            cutover = target.latest_offset()
            unsub = target.subscribe(pattern, sink)
            for msg in target.replay(from_offset=int(from_offset_raw), topic=pattern):
                if msg.offset is not None and msg.offset > cutover:
                    break
                sink.deliver(msg)

        async def _writer() -> None:
            while True:
                env = await queue.get()
                await ws.send_text(env.model_dump_json())

        writer = asyncio.create_task(_writer())
        try:
            while True:
                await ws.receive_text()  # client→server messages are ignored; detects close
        except WebSocketDisconnect:
            pass
        finally:
            sink.close()
            unsub()
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    # ── static frontend (mounted last so /api + /ws win) ──────────────────────

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


def serve(
    config_path: str | Path = "agentbus.yaml",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    codegen_root: str | Path | None = None,
    disable_socket: bool = True,
) -> None:
    """Build the bus + serve the UI (blocks until the server is stopped)."""
    _, uvicorn = _require_fastapi()

    root = Path(codegen_root or Path.cwd()).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    supervisor = BusSupervisor(config_path, disable_socket=disable_socket)
    manager = WorkflowManager(config_path, root)
    app = build_app(supervisor, manager)

    logger.info("AgentBus UI on http://%s:%d (config=%s)", host, port, config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = ["BusSupervisor", "build_app", "serve"]
