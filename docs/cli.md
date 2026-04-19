# CLI Reference

The `agentbus` CLI connects to a running bus over a Unix socket and exposes
real-time introspection commands.

**Install:**

```bash
uv sync --extra cli
```

**Global flags:**

```bash
agentbus [--socket-path PATH] [--log-level LEVEL] [--log-format FORMAT] <command>
```

`--socket-path` defaults to `/tmp/agentbus.sock`. Override if your bus uses a
custom path:

```bash
agentbus --socket-path /tmp/myapp.sock topic list
```

`--log-level` / `--log-format` configure the structured logger once at CLI
entry. `AGENTBUS_LOG_LEVEL`, `AGENTBUS_LOG_FORMAT`, and `AGENTBUS_LOG_FILE`
environment variables work too.

The `topic`, `node`, and `graph` commands require a running bus with a
matching `socket_path`. `chat`, `setup`, `launch`, `daemon`, `doctor`,
`channels`, and `--version` are self-contained.

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
process runs until interrupted. `launch` installs SIGTERM/SIGINT handlers and
applies `bus.shutdown.drain_timeout` (default 5s) for cooperative exit.

---

## chat

Interactive LLM chat session with a full bus in-process. Self-contained — no
running bus required.

```bash
agentbus chat [--config PATH] [--provider NAME] [--model NAME] \
              [--session ID] [--no-memory] [--verbose|--quiet] [--headless]
```

| Flag | Description |
|------|-------------|
| `--config PATH` | Path to `agentbus.yaml`. Default: `./agentbus.yaml`. On first run, an interactive wizard writes one. |
| `--provider NAME` | Override the provider from config. One of `ollama`, `anthropic`, `openai`, `mlx`. |
| `--model NAME` | Override the model. |
| `--session ID` | Resume a session from `~/.agentbus/sessions/<id>/main.json`. |
| `--no-memory` | Disable the memory node for this run (overrides `memory.enabled` in config). |
| `--verbose` | Print `↳ tool_name` lines from `/planning/status` during the run. |
| `--quiet` | Suppress non-essential output. |
| `--headless` | Force stdin/stdout I/O — never launch the interactive TUI. |

**Slash commands** (inside the chat session): `/topics`, `/nodes`, `/graph`,
`/echo <topic>`, `/session [list|new|load <id>]`, `/tools`, `/trace [cid|topic]
[limit]`, `/usage`, `/help`, `/quit`.

---

## daemon

Long-running foreground launch with a pidfile lock plus service-file
generation for systemd / launchd.

```bash
agentbus daemon start agentbus.yaml      # pidfile-locked foreground run
agentbus daemon stop                      # SIGTERM + graceful wait
agentbus daemon status                    # running | stale | absent
agentbus daemon install systemd agentbus.yaml  > ~/.config/systemd/user/agentbus.service
agentbus daemon install launchd agentbus.yaml  > ~/Library/LaunchAgents/com.agentbus.daemon.plist
```

The pidfile (default `~/.agentbus/agentbus.pid`) uses an `fcntl.flock`
advisory lock; a second `start` fails fast with exit code 2 rather than
racing. Templates render `Type=simple` / foreground `ProgramArguments`
entries, so the service manager owns lifecycle and SIGTERM triggers the
graceful drain.

---

## doctor

Diagnostic subcommand. Checks Python version, optional-dep availability,
`~/.agentbus` writability, `agentbus.yaml` validity, socket reachability, and
provider credentials. Exits non-zero on any failure so it is CI-friendly.

```bash
agentbus doctor
```

---

## setup

Full-config interactive wizard. Writes an `agentbus.yaml` from a linear
questionary-backed flow (banner → provider → tools → memory → channels →
doctor probe). Requires the `tui` extra (`uv sync --extra tui`).

```bash
agentbus setup [--config PATH] [--force] [--skip-doctor]
```

| Flag | Description |
|------|-------------|
| `--config PATH` | Target config file. Default: `./agentbus.yaml`. |
| `--force` | Skip the edit/overwrite/cancel prompt and always overwrite. |
| `--skip-doctor` | Skip the post-write `agentbus doctor` probe. |

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Config written successfully (doctor warnings do not fail). |
| `1` | User cancelled — Ctrl-C, declined overwrite, or chose "cancel". |
| `2` | Validation error (channel sub-flow failed, `questionary` not installed). |

**What the wizard does:**

1. Prints the shared block-art AgentBus banner.
2. Detects existing config; offers *edit* (fill defaults from existing
   values), *overwrite* (start blank), or *cancel*.
3. Picks provider + model (pre-filled for the chosen provider).
4. Selects built-in tools (`bash`, `file_read`, `file_write`, `code_exec`).
5. Toggles memory.
6. Loops through channel plugins (Slack, Telegram) — each plugin owns its
   own sub-flow via `ChannelPlugin.interactive_setup(prompter, existing)`.
7. Atomically writes the config, preserving the previous version at
   `agentbus.yaml.bak`.
8. Runs the doctor probe against the new file and prints themed results.

The wizard and `agentbus chat` share a single visual language — same
banner, cyan accent, muted `·` separators, `✗` error glyph.

---

## channels

Multi-channel gateway plugin management. See [Launch Reference](launch.md)
for the `channels:` config shape.

```bash
agentbus channels list                 # registered channel plugins
agentbus channels setup slack          # legacy per-channel wizard
agentbus channels setup telegram
```

For a full themed configuration experience — provider, tools, memory, and
every channel in one flow — prefer `agentbus setup`. `channels setup
<name>` remains for per-channel reconfigure; it writes back to
`channels.<name>` in `agentbus.yaml`.

---

## --version

Print the installed version (from `pyproject.toml` via `importlib.metadata`):

```bash
agentbus --version
```

Also exposed as `agentbus.__version__` on the Python API.

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
