# Web UI (`agentbus ui`)

A browser dashboard to **design workflows**, **introspect** a live bus, and
**replay** durable history — backed by a FastAPI server that runs the bus
in-process.

```bash
uv sync --extra ui            # install FastAPI + uvicorn
uv run agentbus ui            # serve ./agentbus.yaml on http://127.0.0.1:8000
uv run agentbus ui --config myflow.yaml --host 0.0.0.0 --port 9000
```

The UI process builds a `MessageBus` from the config (via the same
`build_bus_from_config` that `agentbus launch` uses), runs `spin()` as a
background task, and serves the dashboard against that live bus. Generated node
and schema code is written under `--workflow-root` (default: the current
directory) as the `agentbus_workflow` package.

## Views

- **Graph** — live React Flow diagram of nodes ↔ topics with publish/subscribe
  edges, refreshed every few seconds (state + message counters per node).
- **Inspect** — pick a topic to see its retained history; toggle *live tail* to
  stream new messages over a WebSocket. Expand any message for the full envelope
  + payload JSON.
- **Replay** — choose a `pattern` and `from offset`, and replay durable history
  gap-free ahead of the live stream (uses the existing `BusServer` resume path;
  durable across restarts when the config uses a `SqliteLog`).
- **Design** — forms to create a topic (with a generated Pydantic schema) or a
  node (with an `on_message` body). Changes are written to `agentbus.yaml` +
  generated Python; click **Apply** to rebuild and restart the bus.
- **Builder** — an LLM helper agent. Describe what you want ("add a topic
  `/orders` and a node that logs each order") and it creates the topics/nodes,
  writes the hook bodies, and can apply the changes. Requires a provider package
  (`uv sync --extra anthropic`); the provider/model come from the `provider:` /
  `model:` keys in the config, defaulting to Anthropic.

## How "design without code" works

Nodes and topic schemas are still real Python — the UI **generates** them:

- A topic's schema becomes a Pydantic model in
  `agentbus_workflow/schemas/<name>.py`.
- A node becomes a `Node` subclass in `agentbus_workflow/nodes/<name>.py` with
  the hook bodies you (or the builder agent) supply.
- `agentbus.yaml` references them by import path, exactly as
  `build_bus_from_config` expects — so the generated workflow is ordinary
  AgentBus config you can edit, commit, and run with `agentbus launch`.

Structured specs are mirrored in `agentbus_workflow/_manifest.json` so editing a
hook regenerates the file from its spec instead of parsing Python.

## HTTP / WS API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | bus running? |
| GET | `/api/graph` | live `BusGraph` (nodes, topics, edges) |
| GET | `/api/topics` / `/api/nodes` | live snapshots |
| GET | `/api/history?topic=/t&n=` | retained messages for a topic |
| GET | `/api/latest-offset` | high-water log offset |
| GET/PUT | `/api/config` | read/replace `agentbus.yaml` |
| POST | `/api/topics` / `/api/nodes` | create (codegen + config) |
| POST | `/api/nodes/{cls}/hooks` | rewrite a hook body |
| POST | `/api/apply` | rebuild + restart the bus |
| POST | `/api/builder` | one turn with the helper agent |
| WS | `/ws/stream?pattern=&from_offset=` | live tap + gap-free replay |

## Developing the frontend

The dashboard is a Vite + React + React Flow app in `agentbus/ui/frontend/`,
built to `agentbus/ui/static/` (served by FastAPI).

```bash
cd agentbus/ui/frontend
npm install
npm run dev      # http://localhost:5173, proxies /api + /ws to :8000
npm run build    # emits ../static/ (commit these for `agentbus ui` to serve)
```

## Notes / limits (v1)

- `Apply` restarts the bus by cancelling the spin task, so node `on_shutdown`
  hooks do not run on reload — fine for design-time, revisit for nodes holding
  external resources.
- The UI-managed bus disables the introspection Unix socket by default to avoid
  colliding with a separately running daemon.
