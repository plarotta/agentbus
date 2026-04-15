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
