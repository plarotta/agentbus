# Launch Reference

AgentBus can be started declaratively from a YAML (or JSON) configuration file.
This lets you wire up a complete bus — topics, nodes, settings — without writing
Python startup code.

---

## Quick start

```yaml
# agentbus.yaml
topics:
  - name: /inbound
    schema: myapp.nodes:InboundChat
    retention: 100

  - name: /outbound
    schema: myapp.nodes:OutboundChat
    retention: 20

nodes:
  - class: myapp.nodes:PlannerNode
  - class: myapp.nodes:ToolExecutorNode
  - class: agentbus.nodes.observer:ObserverNode
```

```bash
agentbus launch agentbus.yaml
# or from Python:
```

```python
from agentbus.launch import launch_sync
launch_sync("agentbus.yaml")
```

---

## Full configuration reference

```yaml
# ── Bus settings ────────────────────────────────────────────────
bus:
  heartbeat_interval: 30.0          # seconds between /system/heartbeat publishes
  introspection_socket: /tmp/agentbus.sock  # set to null to disable
  global_retention: 0               # default retention for topics that don't specify it
  shutdown:
    drain_timeout: 5.0              # seconds to let node loops finish queued
                                    # messages after shutdown is requested
                                    # (external timeout / signal / stop_event)
                                    # before force-cancel
    install_signal_handlers: true   # wire SIGTERM/SIGINT to cooperative exit;
                                    # a second signal escalates to immediate
                                    # cancel. `agentbus launch` defaults to
                                    # true; library embedders default to false.

# ── Topic registrations ─────────────────────────────────────────
topics:
  - name: /inbound                  # required: topic path
    schema: myapp.schemas:InboundChat  # required: import path to the schema class
    retention: 100                  # optional: overrides global_retention
    description: "Incoming messages from the gateway"  # optional

  - name: /tools/request
    schema: agentbus.schemas.common:ToolRequest
    retention: 10

  - name: /tools/result
    schema: agentbus.schemas.common:ToolResult
    retention: 10

  - name: /outbound
    schema: agentbus.schemas.common:OutboundChat
    retention: 20

# ── Node registrations ──────────────────────────────────────────
nodes:
  - class: myapp.nodes:PlannerNode    # required: import path to the Node subclass
    config:                           # optional: passed as **kwargs to __init__
      model: claude-haiku-4-5-20251001
      max_iterations: 20
    concurrency: 4                    # optional: override node.concurrency

  - class: myapp.nodes:ToolExecutorNode

  - class: agentbus.nodes.observer:ObserverNode

# ── MCP stdio servers ──────────────────────────────────────────
# Requires `uv sync --extra mcp`. Tools are discovered at startup and
# registered with the planner under `mcp__<server>__<tool>` names.
mcp_servers:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

# ── Memory node ────────────────────────────────────────────────
# Consolidates chat turns into a local SQLite store with embeddings and
# exposes a `memory_search` tool. Disabled by default.
memory:
  enabled: true
  provider: ollama                  # currently only "ollama"
  model: nomic-embed-text
  base_url: http://localhost:11434
  db_path: ~/.agentbus/memory.db

# ── Multi-channel gateways ─────────────────────────────────────
# Plugin-per-channel architecture; each block is validated against the
# plugin's ConfigModel before the bus starts. `enabled: false` skips a
# channel. Install the extras you need: `uv sync --extra slack`,
# `uv sync --extra telegram`, or `uv sync --extra channels` for both.
channels:
  slack:
    enabled: true
    app_token: ${SLACK_APP_TOKEN}   # xapp-… (connections:write scope)
    bot_token: ${SLACK_BOT_TOKEN}   # xoxb-…
    allowed_channels: ["C01234"]    # empty list = allow all
    allowed_senders: ["U01234"]
  telegram:
    enabled: true
    bot_token: ${TELEGRAM_BOT_TOKEN}
    allowed_chats: [12345]          # int chat IDs
    long_poll_timeout_s: 25

# ── Tool sandbox (applies to `bash` and `code_exec`) ───────────
# Sandbox is on by default — omitting this block still gets subprocess
# isolation with conservative limits (CPU=30s, memory=512 MiB, output
# capped at 256 KiB, scrubbed env, per-invocation tempdir as cwd).
# Permission policy is evaluated *above* the sandbox — a denied command
# never reaches the child process. Docker backend requires the `docker`
# binary on PATH and enables network isolation + a read-only rootfs.
sandbox:
  backend: subprocess                # "subprocess" (default) | "docker"
  cpu_seconds: 30                    # RLIMIT_CPU cap in the child
  memory_mb: 512                     # RLIMIT_AS cap (best-effort on macOS)
  max_output_bytes: 262144           # 256 KiB; stdout above this is truncated
  workdir: null                      # null = per-invocation tempdir
  env_passthrough: []                # extra env vars to let through the scrub
  image: python:3.12-slim            # docker-only
  network: false                     # docker-only; `false` adds --network=none

# ── Tool permission policy ─────────────────────────────────────
# Per-tool gate applied by ChatToolNode before handler dispatch. Deny
# rules short-circuit before approval prompts. File-path rules resolve
# both target and root before comparison so `../` cannot escape allowlists.
# Omit `permissions:` entirely to keep the pre-existing default (allow all).
permissions:
  bash:
    mode: approval_required          # allow | deny | approval_required
    deny_commands: ["rm", "curl"]    # prefix match on leading token
    allow_commands: ["ls", "cat"]    # empty = allow all non-denied
  file_write:
    mode: allow
    deny_paths: ["~/.ssh", "/etc"]
    allow_paths: ["~/workspace"]
  file_read:
    mode: allow
```

---

## Schema import paths

The `schema` field in topic configuration and the `class` field in node
configuration both accept Python import paths. Two formats are supported:

```yaml
schema: myapp.schemas:InboundChat       # colon-separated (preferred)
schema: myapp.schemas.InboundChat       # dot-separated (last component is the attribute)
```

The import path must be resolvable in the current Python environment. Use the
colon format for clarity.

**Built-in schema paths:**

| Schema | Import path |
|--------|-------------|
| `InboundChat` | `agentbus.schemas.common:InboundChat` |
| `OutboundChat` | `agentbus.schemas.common:OutboundChat` |
| `ToolRequest` | `agentbus.schemas.common:ToolRequest` |
| `ToolResult` | `agentbus.schemas.common:ToolResult` |

**Built-in node paths:**

| Node | Import path |
|------|-------------|
| `ObserverNode` | `agentbus.nodes.observer:ObserverNode` |
| `GatewayNode` (abstract) | `agentbus.gateway:GatewayNode` |

---

## Node `config`

When a node's `__init__` accepts keyword arguments, pass them via `config`:

```python
# myapp/nodes.py
class PlannerNode(Node):
    name = "planner"

    def __init__(self, *, model: str = "claude-haiku-4-5-20251001", max_iterations: int = 25):
        self._model = model
        self._max_iterations = max_iterations
```

```yaml
nodes:
  - class: myapp.nodes:PlannerNode
    config:
      model: claude-opus-4-6
      max_iterations: 30
```

---

## Python API

### `build_bus_from_config(config)`

```python
from agentbus.launch import build_bus_from_config

with open("agentbus.yaml") as f:
    import yaml
    config = yaml.safe_load(f)

bus = build_bus_from_config(config)
# bus is fully registered — call bus.spin() yourself
await bus.spin()
```

### `launch(config_path)`

```python
from agentbus.launch import launch

await launch("agentbus.yaml")  # loads config and calls spin()
```

### `launch_sync(config_path)`

```python
from agentbus.launch import launch_sync

launch_sync("agentbus.yaml")  # asyncio.run(launch(...))
```

---

## Example: multi-agent pipeline

```yaml
bus:
  heartbeat_interval: 60.0
  introspection_socket: /tmp/pipeline.sock

topics:
  - name: /docs/raw
    schema: myapp.schemas:RawDocument
    retention: 50

  - name: /docs/analyzed
    schema: myapp.schemas:AnalyzedDocument
    retention: 50

  - name: /docs/output
    schema: myapp.schemas:FinalDocument
    retention: 50

nodes:
  - class: myapp.nodes:IngestNode
    config:
      source_dir: /data/input

  - class: myapp.nodes:AnalyzerNode
    concurrency: 4

  - class: myapp.nodes:FormatterNode

  - class: agentbus.nodes.observer:ObserverNode
```

---

## JSON format

YAML is preferred but JSON is also accepted:

```json
{
  "bus": {
    "heartbeat_interval": 30.0,
    "introspection_socket": "/tmp/agentbus.sock"
  },
  "topics": [
    {"name": "/inbound", "schema": "myapp.schemas:InboundChat", "retention": 100}
  ],
  "nodes": [
    {"class": "myapp.nodes:PlannerNode"}
  ]
}
```
