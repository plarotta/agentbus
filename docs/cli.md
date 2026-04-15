# CLI Reference

The `agentbus` CLI connects to a running bus over a Unix socket and exposes
real-time introspection commands.

**Install:**

```bash
uv sync --extra cli
```

**Global flag:**

```bash
agentbus [--socket-path PATH] <command>
```

`--socket-path` defaults to `/tmp/agentbus.sock`. Override if your bus uses a
custom path:

```bash
agentbus --socket-path /tmp/myapp.sock topic list
```

The CLI only works while a bus is running with a matching `socket_path`.

---

## topic list

List all registered topics with their schema, retention setting, and
subscriber count.

```bash
agentbus topic list
```

**Output:** JSON array of `TopicInfo` objects.

```json
[
  {
    "message_count": 42,
    "name": "/inbound",
    "queue_depths": {"planner": 0},
    "retention": 100,
    "schema_name": "InboundChat",
    "subscriber_count": 1
  },
  {
    "message_count": 42,
    "name": "/outbound",
    "queue_depths": {},
    "retention": 20,
    "schema_name": "OutboundChat",
    "subscriber_count": 0
  }
]
```

---

## topic echo

Stream messages from a topic as they arrive.

```bash
agentbus topic echo <topic> [--n N]
```

| Argument | Description |
|----------|-------------|
| `topic` | Full topic name, e.g. `/tools/request` |
| `--n N` | Stop after N messages. Omit to stream indefinitely. |

**Output:** Newline-delimited JSON, one message per line.

```bash
agentbus topic echo /tools/request --n 5
```

```json
{"correlation_id": "abc123", "id": "...", "payload": {"params": {}, "tool": "calculate"}, "source_node": "planner", "timestamp": "...", "topic": "/tools/request"}
```

**Use cases:**
- Watch tool calls as the agent runs
- Debug unexpected messages
- Verify message schemas in production

---

## node list

List all registered nodes with their state, subscriptions, and publications.

```bash
agentbus node list
```

**Output:** JSON array of `NodeInfo` objects.

```json
[
  {
    "concurrency": 1,
    "concurrency_mode": "serial",
    "errors": 0,
    "messages_published": 12,
    "messages_received": 12,
    "name": "planner",
    "publications": ["/outbound", "/tools/request"],
    "state": "RUNNING",
    "subscriptions": ["/inbound"]
  }
]
```

---

## node info

Get detailed runtime information for a single node.

```bash
agentbus node info <name>
```

```bash
agentbus node info planner
```

**Output:** Single `NodeInfo` JSON object.

---

## graph

Get the pub/sub wiring graph.

```bash
agentbus graph [--format FORMAT]
```

| Format | Description |
|--------|-------------|
| `json` (default) | `BusGraph` as JSON |
| `mermaid` | Mermaid `graph TD` diagram |
| `dot` | Graphviz DOT format |

**JSON output:**

```bash
agentbus graph
```

```json
{
  "edges": [
    {"direction": "pub", "node": "planner", "topic": "/tools/request"},
    {"direction": "sub", "node": "tool_executor", "topic": "/tools/request"}
  ],
  "nodes": [...],
  "topics": [...]
}
```

**Mermaid output:**

```bash
agentbus graph --format mermaid
```

```
graph TD
  "planner" --> "/tools/request"
  "/tools/request" --> "tool_executor"
  "tool_executor" --> "/tools/result"
  "planner" --> "/outbound"
  "/inbound" --> "planner"
```

Pipe into a Mermaid renderer or paste into a GitHub comment to get a visual
topology diagram.

**DOT output:**

```bash
agentbus graph --format dot | dot -Tpng -o graph.png
```

---

## launch

Launch a bus from a YAML (or JSON) configuration file. See
[Launch Reference](launch.md) for the config format.

```bash
agentbus launch agentbus.yaml
```

This is equivalent to calling `launch_sync("agentbus.yaml")` in Python. The
process runs until interrupted.

---

## Programmatic access

All CLI commands are also available as Python functions:

```python
from agentbus.cli import (
    topic_list,
    topic_echo,
    node_list,
    node_info,
    graph,
)

# Synchronous — uses asyncio.run() internally
print(topic_list(socket_path="/tmp/myapp.sock"))
print(graph(format="mermaid"))
```

For use inside an already-running event loop, use the async primitive directly:

```python
from agentbus.cli import _socket_request, _format_json

# Inside an async context (e.g. a test):
raw = await _socket_request({"cmd": "graph"}, socket_path=socket_path)
print(_format_json(raw))
```

---

## Socket protocol

The introspection server uses newline-delimited JSON over a Unix domain socket.
You can speak to it with any tool:

```bash
echo '{"cmd": "topics"}' | nc -U /tmp/agentbus.sock
```

**Commands:**

| Command | Payload | Response |
|---------|---------|----------|
| `topics` | `{"cmd": "topics"}` | `list[TopicInfo]` |
| `nodes` | `{"cmd": "nodes"}` | `list[NodeInfo]` |
| `node_info` | `{"cmd": "node_info", "name": "planner"}` | `NodeInfo` |
| `graph` | `{"cmd": "graph"}` | `BusGraph` |
| `history` | `{"cmd": "history", "topic": "/inbound", "n": 10}` | `list[Message]` |
| `echo` | `{"cmd": "echo", "topic": "/tools/request", "n": 5}` | stream of `Message` lines |

For `echo`, the server streams newline-delimited JSON until `n` messages have
been delivered, then closes the connection. If `n` is omitted, the server
streams indefinitely until the client disconnects.
