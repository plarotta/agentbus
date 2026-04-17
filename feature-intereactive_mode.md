# Feature spec: `agentbus chat`

> Interactive CLI mode where the user talks to the PlannerNode directly. The bus runs behind the scenes. Introspection is one keystroke away.

**Status:** Proposed
**Priority:** P0 — this is the default entry point for new users
**Dependencies:** Core bus, harness, at least one provider, CLI scaffold

---

## 1. Summary

`agentbus chat` launches a TUI session where the user converses with a harness-powered PlannerNode. Internally, every tool call routes through the message bus as typed pub/sub messages. The user doesn't see or know about the bus unless they ask. Slash commands expose introspection on demand — topics, message flow, node state, topology graph — then disappear when dismissed.

The user experience is: install, run, chat. The architecture is invisible until you want it.

```
$ agentbus chat
AgentBus v0.1.0 • provider: ollama/llama3.1:8b • 4 tools loaded
Type /help for commands

> what's in this directory?

Listing files in the current directory...

  README.md    agentbus.yaml    src/    tests/    examples/

There are 5 items: README.md, agentbus.yaml, and three directories.

> /topics
┌─────────────────┬─────────────┬───────┬────────┬─────────┐
│ topic           │ schema      │ subs  │ msgs   │ rate    │
├─────────────────┼─────────────┼───────┼────────┼─────────┤
│ /inbound        │ InboundChat │ 1     │ 1      │ 0.1/s   │
│ /tools/request  │ ToolRequest │ 2     │ 1      │ 0.1/s   │
│ /tools/result   │ ToolResult  │ 1     │ 1      │ 0.1/s   │
│ /outbound       │ OutboundChat│ 0     │ 1      │ 0.1/s   │
└─────────────────┴─────────────┴───────┴────────┴─────────┘

> /echo /tools/request
[live tail — Ctrl-C to stop]

> list the python files and count the lines in each

[14:23:01] planner → /tools/request
  ToolRequest(tool="bash", params={"command": "find . -name '*.py' | head -20"})

[14:23:02] planner → /tools/request
  ToolRequest(tool="bash", params={"command": "wc -l src/*.py"})

^C

Found 8 Python files with a total of 1,247 lines...
```

---

## 2. Entry points

### `agentbus chat`

Launch interactive mode using `agentbus.yaml` in the current directory (or `--config path`).

```
agentbus chat [OPTIONS]

Options:
  --config PATH         Path to agentbus.yaml (default: ./agentbus.yaml)
  --provider NAME       Override provider from config (ollama, mlx, anthropic, openai)
  --model NAME          Override model from config
  --session ID          Resume a previous session by ID
  --no-memory           Disable MemoryNode even if configured
  --verbose             Show tool dispatches inline (without needing /echo)
  --headless            No TUI — plain stdin/stdout, for piping
```

### `agentbus chat --init`

If no `agentbus.yaml` exists, prompt the user interactively (no GUI required):

```
$ agentbus chat
No agentbus.yaml found. Quick setup:

Provider [ollama]: 
Model [llama3.1:8b-instruct]: 
Enable tools: bash, file_read, file_write, code_exec? [Y/n]: 
Enable memory (RAG + vector store)? [y/N]: 

Wrote agentbus.yaml. Starting chat...
```

This replaces the web GUI for the common case. The web wizard (`agentbus init`) remains available for complex topologies.

---

## 3. TUI layout

Use `textual` for the terminal UI. Two modes:

### Normal mode (default)

Full-screen single pane. Conversation scrolls up. Input at the bottom. Minimal chrome.

```
┌──────────────────────────────────────────────────────────┐
│ AgentBus • ollama/llama3.1:8b • session: a3f8c2         │
├──────────────────────────────────────────────────────────┤
│                                                          │
│ > what's in this directory?                              │
│                                                          │
│ There are 5 items in the current directory:              │
│ README.md, agentbus.yaml, and three directories          │
│ (src, tests, examples).                                  │
│                                                          │
│ > explain the main module                                │
│                                                          │
│ [thinking... dispatching file_read]                      │
│                                                          │
├──────────────────────────────────────────────────────────┤
│ >                                                        │
└──────────────────────────────────────────────────────────┘
```

The `[thinking... dispatching file_read]` status line appears while the harness is in its loop. It updates in real time as the harness state changes: `thinking` → `dispatching bash` → `awaiting result` → `thinking` → `responding`. This is fed by `/planning/status` messages.

### Inspect mode (toggle with `/inspect` or `Ctrl-I`)

Split pane. Top: conversation (unchanged). Bottom: live topic feed.

```
┌──────────────────────────────────────────────────────────┐
│ AgentBus • ollama/llama3.1:8b • session: a3f8c2         │
├──────────────────────────────────────────────────────────┤
│ > explain the main module                                │
│                                                          │
│ The main module in src/main.py handles CLI argument      │
│ parsing and launches the bus from config...              │
│                                                          │
├──────────────────── inspect ─────────────────────────────┤
│ [/tools/request] 14:23:01 planner →                      │
│   ToolRequest(tool="file_read", params={"path":          │
│   "src/main.py"})                                        │
│ [/tools/result] 14:23:01 file_read →                     │
│   ToolResult(output="#!/usr/bin/env python3...")          │
│ [/planning/status] 14:23:02 planner →                    │
│   PlannerStatus(event="thinking", iteration=2,           │
│   context_tokens=3847, context_capacity=0.19)            │
├──────────────────────────────────────────────────────────┤
│ >                                                        │
└──────────────────────────────────────────────────────────┘
```

The inspect pane defaults to tailing all non-system topics. User can filter:
- `/inspect /tools/*` — only tool traffic
- `/inspect /planning/status` — only harness state
- `/inspect` with no args — toggle off

Inspect mode is purely observational. It does not affect the conversation or the bus.

---

## 4. Slash commands

All commands are prefixed with `/`. Anything without a `/` is a user message sent to the planner.

### Introspection

| Command | Action |
|---------|--------|
| `/inspect [topic_pattern]` | Toggle inspect pane. Optional topic filter. |
| `/topics` | Print topic list with stats (inline, not in inspect pane). |
| `/nodes` | Print node list with state, latency, error counts. |
| `/graph` | Print mermaid topology diagram to conversation. |
| `/echo <topic> [n]` | Print last N messages from topic retention buffer. Default: 5. |
| `/history [n]` | Print last N messages across all topics. Default: 10. |

### Session

| Command | Action |
|---------|--------|
| `/session` | Print current session ID, turn count, context usage. |
| `/session list` | List all saved sessions with timestamps. |
| `/session fork` | Fork current session at this point. Continues in the fork. |
| `/session switch <id>` | Switch to a different session (preserves current). |
| `/compact` | Force a full compaction of the current session. |

### Control

| Command | Action |
|---------|--------|
| `/provider [name] [model]` | Show or switch the active provider/model. No args = show current. |
| `/tools` | List available tools with their ToolNode status. |
| `/clear` | Clear the screen (not the session). |
| `/help` | Print command reference. |
| `/quit` or `Ctrl-D` | Graceful shutdown: drain queues, save session, exit. |

### Debugging

| Command | Action |
|---------|--------|
| `/replay <topic> <msg_id>` | Re-publish a message from the retention buffer. For re-triggering tool calls. |
| `/pause` | Pause all nodes except observer. Messages queue but don't process. |
| `/resume` | Resume processing. Queued messages flush. |
| `/breakers` | Show all circuit breaker states (open/closed, failure counts). |

---

## 5. Status line behavior

The status line between conversation and input shows the harness state in real time. It subscribes to `/planning/status` internally.

| Harness state | Status line |
|---------------|-------------|
| Idle (waiting for input) | *(hidden — no status line)* |
| LLM call in progress | `[thinking...]` |
| Tool dispatched | `[dispatching: bash → "find . -name '*.py'"]` |
| Awaiting tool result | `[awaiting: bash]` |
| Multiple tools in flight | `[awaiting: bash, file_read (2 tools)]` |
| Compacting | `[compacting context... 84% → target 50%]` |
| Model demotion | `[provider fallback: ollama → anthropic]` |
| Error | `[error: tool timeout on bash after 30s]` |

The status line disappears when the response starts streaming. Tool names and short param previews help the user understand what's happening without needing inspect mode.

---

## 6. Message flow

User types a message. Here's what happens:

```
1. TUI captures input text
2. TUI publishes Message[InboundChat] to /inbound
3. PlannerNode.on_message receives it
4. PlannerNode calls self.harness.run(text)
5. Harness enters agent loop:
   a. Calls provider (LLM)
   b. If LLM returns tool calls:
      - Harness calls tool_executor callback
      - PlannerNode.tool_executor publishes ToolRequest to /tools/request
      - PlannerNode awaits correlated reply on /tools/result
      - ToolNode processes request, publishes ToolResult
      - Harness receives result, feeds back to LLM
      - Loop continues
   c. If LLM returns text (no tool calls):
      - Harness returns response
6. PlannerNode publishes Message[OutboundChat] to /outbound
7. TUI subscribes to /outbound, renders response
```

The TUI is itself a node — a lightweight display node that publishes to `/inbound` and subscribes to `/outbound`. This is the gateway pattern from the PRD, realized as a terminal interface instead of Slack/Discord.

```python
class TUINode(Node):
    """The terminal interface as a bus node."""
    name = "tui"
    subscriptions = ["/outbound", "/planning/status"]
    publications = ["/inbound"]

    async def on_message(self, msg: Message) -> None:
        if msg.topic == "/outbound":
            self.display_response(msg.payload.text)
        elif msg.topic == "/planning/status":
            self.update_status_line(msg.payload)
```

---

## 7. `--verbose` mode

For users who want tool visibility without the full inspect pane. Tool dispatches print inline in the conversation:

```
> find all TODO comments in the codebase

  ↳ bash: grep -rn "TODO" src/
  ↳ bash: grep -rn "TODO" tests/

Found 7 TODO comments across 4 files:
  ...
```

The `↳` lines are dimmed/secondary color. They show the tool name and a truncated param preview. No pane split, no mode toggle. Just inline transparency.

Enabled by default if the terminal is wider than 100 columns. Can be forced on/off with `--verbose` / `--quiet`.

---

## 8. `--headless` mode

No TUI. Plain stdin/stdout. For piping and scripting.

```bash
# single query
echo "list files in src/" | agentbus chat --headless

# pipe into another tool
agentbus chat --headless <<< "summarize README.md" | pbcopy

# interactive but without TUI (raw line mode)
agentbus chat --headless
> what time is it?
I don't have access to the current time, but I can check...
```

Headless mode:
- No color, no status line, no inspect pane
- Input from stdin, output to stdout
- Tool dispatches go to stderr (visible in terminal, excluded from pipes)
- Session still persists
- Bus still runs (introspection available via `agentbus topic list` in another terminal)
- Exit on EOF or `/quit`

---

## 9. Streaming

LLM responses stream token-by-token to the TUI. The harness yields chunks as they arrive from the provider. The TUI renders them incrementally.

```python
# Inside PlannerNode, the harness exposes a streaming variant:
async for chunk in self.harness.run_stream(text):
    if chunk.type == "text":
        await self.publish("/outbound/stream", StreamChunk(text=chunk.text))
    elif chunk.type == "status":
        await self.publish("/planning/status", PlannerStatus(...))
```

The TUI subscribes to `/outbound/stream` for incremental rendering and `/outbound` for the final complete message. Streaming is best-effort — if the TUI falls behind, it catches up (no backpressure on display).

For `--headless` mode, chunks write directly to stdout with no buffering.

---

## 10. Session persistence on exit

On `/quit` or `Ctrl-D`:

1. Harness saves session to `~/.agentbus/sessions/<session_id>/main.json`
2. Bus drains all queues (5s timeout)
3. Bus calls `on_shutdown()` on all nodes
4. TUI prints session summary:

```
Session a3f8c2 saved (12 turns, 4,231 tokens)
Resume with: agentbus chat --session a3f8c2
```

On `Ctrl-C` (interrupt):

1. If harness is mid-loop (waiting for LLM or tool): cancel the current operation, save session at the last complete turn, exit.
2. If idle: same as `/quit`.
3. Double `Ctrl-C` within 1 second: hard exit, no save.

---

## 11. First-run experience

No `agentbus.yaml`, no prior sessions, user just installed:

```
$ pip install agentbus
$ agentbus chat

Welcome to AgentBus.

No config found. Let's set up quickly.

? Provider
  ❯ ollama (local — requires ollama running)
    mlx (local — Apple Silicon only)
    anthropic (API key required)
    openai (API key required)

? Model: llama3.1:8b-instruct
? Enable default tools (bash, file_read, file_write)? Yes

✓ Wrote agentbus.yaml
✓ Starting bus with 4 nodes (planner, bash, file_read, file_write)

>
```

Total time from install to first message: under 30 seconds. No browser, no GUI, no config file editing. The interactive prompts use `questionary` or similar — arrow keys, enter, done.

After first run, `agentbus chat` uses the existing `agentbus.yaml` and goes straight to the prompt.

---

## 12. Implementation plan

### Phase 1: Headless mode (1-2 days)

- Wire TUINode as a stdin/stdout node
- Publish to `/inbound` on each input line
- Subscribe to `/outbound`, print responses
- Integrate with existing bus + harness
- `--headless` flag only, no TUI dependency

This is the minimum viable demo. It proves the bus works end-to-end with a human in the loop.

### Phase 2: Verbose mode + slash commands (2-3 days)

- Add `--verbose` inline tool dispatch rendering
- Implement slash command parser
- `/topics`, `/nodes`, `/echo`, `/history`, `/help`, `/quit`
- `/session`, `/compact`, `/provider`
- Status line (non-TUI — just a rewritable terminal line via `\r`)

This is the power-user version. Still no `textual` dependency.

### Phase 3: Full TUI with inspect pane (3-5 days)

- `textual` app with split pane layout
- Inspect mode toggle (`/inspect`, `Ctrl-I`)
- Live topic feed in bottom pane with filtering
- Streaming response rendering
- Session management UI (`/session list`, `/session fork`)

This is the polished version. Optional dependency — falls back to Phase 2 if `textual` isn't installed.

### Phase 4: First-run wizard (1 day)

- Detect missing `agentbus.yaml`
- Interactive provider/model/tool prompts
- Write config and launch

---

## 13. Testing

### Unit tests

- Slash command parser: input → parsed command + args
- TUINode: mock bus, assert publishes to `/inbound` and subscribes to `/outbound`
- Status line renderer: PlannerStatus → expected string

### Integration tests

- Full loop: TUINode publishes InboundChat → PlannerNode processes → OutboundChat arrives at TUINode
- Uses `spin_once()` and fake provider (from HarnessDeps)
- No real LLM, no real tools — all injected fakes
- Assert message flow through the bus matches expected sequence

### Manual test script

```bash
# smoke test: does it start and respond?
echo "hello" | agentbus chat --headless --config test_config.yaml

# tool dispatch test: does it call tools?
echo "list files in /tmp" | agentbus chat --headless --verbose 2>tools.log
grep "bash" tools.log  # should contain tool dispatch

# introspection test: do slash commands work?
printf "/topics\n/quit\n" | agentbus chat --headless
```

---

## 14. Out of scope for v1

- Web-based chat interface (terminal only)
- Multi-user / shared sessions
- Voice input/output
- Auto-suggest / tab completion on slash commands (v2 candidate)
- Plugin system for custom slash commands (v2 candidate)
- Image/file rendering in the TUI (show paths, not content)