# Manual Testing Plan

A practical end-to-end checklist for verifying every user-facing surface of AgentBus before a release. Run top-to-bottom on a fresh clone (or at least a fresh shell). Pairs well with the automated test suite (`uv run pytest tests/`) — the suite proves correctness, this plan proves *feel*.

**Setup once per run:**

```bash
uv sync --extra all
export ANTHROPIC_API_KEY=...      # or OPENAI_API_KEY for the openai path
ollama serve &                     # only if testing the ollama/memory paths
```

Track progress by ticking each box as you finish. Expected behavior is called out in *italics*; red-flag behavior in **bold**.

---

## 1. Pre-flight

- [ ] `uv run agentbus --version` → prints `agentbus 0.2.0` (or current)
- [ ] `uv run agentbus doctor` → all checks ✓ or explicit warnings for optional deps you haven't installed. **Fail:** anything reporting `fail` for a dep you have installed.
- [ ] `uv run agentbus doctor` without an `ANTHROPIC_API_KEY` set → provider-creds check warns, not crashes.

---

## 2. Setup wizard

- [ ] Move `agentbus.yaml` aside: `mv agentbus.yaml agentbus.yaml.saved`
- [ ] `uv run agentbus setup` → banner renders with ~2 blank lines of breathing room above it, then linear flow: provider → model → tools → memory → channels → doctor probe → outro.
- [ ] Pick `anthropic` provider, default model, enable all tools, skip memory and channels. Wizard writes `agentbus.yaml`. *Verify contents:* `cat agentbus.yaml`.
- [ ] Re-run `uv run agentbus setup`. Now prompts edit / overwrite / cancel. Pick **cancel** — exit code 1, no write.
- [ ] Pick **overwrite** — a `agentbus.yaml.bak` is written alongside.
- [ ] Pick **edit** — prompts fill with existing values as defaults.
- [ ] Ctrl-C mid-wizard → clean exit, no partial file written.

---

## 3. Chat — TUI mode (default)

Start a fresh session from the wizard-written config.

```bash
uv run agentbus chat
```

- [ ] Banner appears with the two blank lines of spacing above the block-art logo.
- [ ] Bottom toolbar shows `AgentBus · <provider>/<model> · N tools · session <id>` on a **dark** background (not light gray). Text is readable.
- [ ] Token meter renders at the right of the toolbar. Starts as `0 tok` or `0 / 200k tok (0%)` once a harness call has happened.
- [ ] Type a prompt. While waiting, a rich-style **dot spinner** appears under the `❯` line with text `thinking…`. If a tool dispatches mid-flight, the spinner label updates to `↳ <tool_name>`.
- [ ] Spinner disappears when the response renders. Response renders as markdown.
- [ ] Token meter climbs. Colors step green → yellow (≥60%) → red (≥85%) as the window fills (harder to verify — try a very small `context_window` by hand if you want).
- [ ] Input history persists across sessions: exit, re-run, hit Up-arrow — see prior prompts.
- [ ] Ctrl-D exits cleanly. Ctrl-C clears the current line without exiting.

---

## 4. Chat — slash commands

All inside a live `agentbus chat` TUI session:

- [ ] `/help` → lists all commands.
- [ ] `/topics` → shows the registered topics with retention + subscriber count.
- [ ] `/nodes` → shows `chat_planner`, `chat_tools`, observer, etc.
- [ ] `/graph` → mermaid diagram renders.
- [ ] `/echo /tools/request` → streams live tool requests (hit it, then send another prompt).
- [ ] `/trace` → walks the message log for the most recent correlation_id; shows planner → tool → planner path.
- [ ] `/usage` → token breakdown by role (user/assistant/system/tool). Totals match toolbar meter.
- [ ] `/tools` → lists tool schemas visible to the planner (builtins + MCP + memory if enabled).
- [ ] `/session list` → shows existing sessions. `/session new` starts fresh. `/session load <id>` resumes.
- [ ] `/quit` exits cleanly.

---

## 5. Chat — headless mode

```bash
uv run agentbus chat --headless
```

- [ ] Banner renders with the same spacing. **No toolbar** (headless).
- [ ] Prompt loop works via plain stdin/stdout.
- [ ] `--verbose` prints `↳ tool_name` inline while tools dispatch.
- [ ] Pipe input works: `echo "what is 2+2" | uv run agentbus chat --headless --quiet`.

---

## 6. Sandbox — subprocess backend (default)

Enable `bash` in `agentbus.yaml`. Inside chat:

- [ ] Ask the agent to run `echo hello` via bash. Output should contain `hello`.
- [ ] Ask it to run `pwd`. Output path contains `agentbus-sandbox-` (the per-invocation tempdir). **Fail:** output shows your real working directory.
- [ ] Ask it to run `env`. **`ANTHROPIC_API_KEY` must NOT appear.** `PATH`, `HOME`, `LANG` do.
- [ ] Ask it to run `sleep 60` with a 5s tool timeout. Sandbox kills the process group after ~5s and returns `Error: command timed out`.
- [ ] Ask it to run `yes | head -c 10000000` (10 MB of `y`s). Output is truncated at 256 KiB with a `[output truncated at 262144 bytes]` note.
- [ ] Exit chat, check `ls /tmp | grep agentbus-sandbox-` → tempdirs cleaned up, none lingering.

**Docker backend (optional — requires `docker` on PATH):**

Edit `agentbus.yaml`:

```yaml
sandbox:
  backend: docker
  image: python:3.12-slim
  network: false
```

- [ ] Re-run `agentbus chat`, ask for a bash tool call. Sandbox spawns `docker run --rm --read-only ...`. First call pulls the image.
- [ ] Inside the docker backend, `curl https://example.com` fails (no network).
- [ ] Set `network: true`, retry — `curl` works.
- [ ] `docker ps -a` shows no lingering containers after the chat exits.

---

## 7. Permissions

Edit `agentbus.yaml`:

```yaml
permissions:
  bash:
    mode: approval_required
    deny_commands: ["rm"]
  file_write:
    mode: allow
    deny_paths: ["~/.ssh", "/etc"]
    allow_paths: ["~/agentbus-playground"]
```

- [ ] Run `uv run agentbus chat --headless` (approval prompts only wire up in headless mode). Ask the agent to `ls /`. Prompt appears on stdin — approve with `y`. Command runs.
- [ ] Ask it to `rm /tmp/foo`. **No approval prompt** — denied outright by the deny rule (verify this short-circuits before the approval).
- [ ] Ask the agent to write to `~/.ssh/authorized_keys`. Deny rule fires, returns `Error: permission denied: ...`.
- [ ] Ask it to write to `~/agentbus-playground/test.txt`. Allowed.
- [ ] Ask it to write to `~/agentbus-playground/../../etc/passwd`. Path-traversal escape attempt — deny rule still fires (the path is resolved before comparison).

---

## 8. Memory node

Requires Ollama running with an embedding model:

```bash
ollama pull nomic-embed-text
```

In `agentbus.yaml`:

```yaml
memory:
  enabled: true
  provider: ollama
  model: nomic-embed-text
```

- [ ] Start chat. Say `"Remember that my favorite color is cyan."` — regular reply.
- [ ] Say `"What's my favorite color?"` — the agent calls `memory_search` (visible in `/usage` or `--verbose`) and replies `cyan`.
- [ ] Kill chat, restart with `--no-memory`. Ask the same question — agent no longer knows.
- [ ] `ls -lh ~/.agentbus/memory.db` → file exists and is growing.

---

## 9. MCP gateway

Install `uv sync --extra mcp`. In `agentbus.yaml`:

```yaml
mcp_servers:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

- [ ] Start chat. `/tools` → lists `mcp__filesystem__*` alongside builtins.
- [ ] Ask the agent to list files in `/tmp` via the MCP tool. Works.
- [ ] Stop the MCP server externally mid-call (kill the npx process) — chat doesn't crash; tool call surfaces `ToolResult.error`.

---

## 10. Swarm (hub-and-spoke)

Run the bundled example:

```bash
uv run python examples/swarm/main.py
```

- [ ] Example completes end-to-end. Coordinator LLM dispatches to each sub-agent via `dispatch_subagent`. Each sub-agent runs in its own Harness.
- [ ] Inspect `/swarm/<name>/inbound` + `/swarm/<name>/outbound` via `agentbus topic list` (from a second terminal) while it runs.
- [ ] Sub-agents never publish to each other's inbound topic — all handoffs route through the coordinator.

---

## 11. Channels — Slack

Requires `uv sync --extra slack`, an app token (`xapp-…`), a bot token (`xoxb-…`), and a test Slack workspace.

- [ ] `uv run agentbus channels setup slack` writes the `channels.slack` block.
- [ ] `uv run agentbus doctor` → Slack probe reports `ok` (auth.test succeeds).
- [ ] `uv run agentbus launch agentbus.yaml` → gateway connects. `/system/channels` topic sees `ChannelStatus(state=connected)` (use `agentbus topic echo /system/channels` from a second terminal).
- [ ] DM the bot — response threads correctly.
- [ ] Mention the bot in a channel outside `allowed_channels` — **no reply**.
- [ ] Rotate the token to invalid — gateway publishes `state=error` and stops retrying (no hot loop).
- [ ] Send a very long prompt that triggers >8000-char reply — chunked cleanly at paragraph/line boundaries.

---

## 12. Channels — Telegram

Requires `uv sync --extra telegram` and a bot token from BotFather.

- [ ] `uv run agentbus channels setup telegram` writes the `channels.telegram` block.
- [ ] `uv run agentbus doctor` → Telegram probe reports `ok`.
- [ ] `uv run agentbus launch agentbus.yaml` → gateway starts long-polling.
- [ ] DM the bot — reply arrives; first chunk carries `reply_to_message_id`, follow-ups don't.
- [ ] Message from a `chat_id` outside `allowed_chats` — no reply.
- [ ] Kill network briefly (wifi off 15s) — gateway retries with exponential backoff (see logs), recovers when network returns.

---

## 13. Daemon

- [ ] `uv run agentbus daemon start agentbus.yaml` → runs in foreground with pidfile locked.
- [ ] From another shell: `uv run agentbus daemon status` → `running`.
- [ ] Second `daemon start` attempt → exit code 2 (pidfile lock held).
- [ ] `uv run agentbus daemon stop` → SIGTERM, graceful drain, pidfile cleaned up.
- [ ] `uv run agentbus daemon install systemd agentbus.yaml` → prints a valid `.service` unit with the absolute `agentbus` path baked in.

---

## 14. Launch (YAML-first)

```bash
uv run agentbus launch agentbus.yaml
```

- [ ] Bus starts. All configured nodes appear in `agentbus node list` from a second terminal.
- [ ] SIGTERM → graceful drain (up to `bus.shutdown.drain_timeout`), session flushed, socket cleaned up.
- [ ] Second SIGTERM during drain → immediate cancel (escalation).

---

## 15. CLI introspection (against a running bus)

From a second terminal while `agentbus chat` or `agentbus launch` is running:

- [ ] `uv run agentbus topic list` → shows all topics with retention + subscriber counts.
- [ ] `uv run agentbus topic echo /tools/request` → streams requests live as you prompt the chat.
- [ ] `uv run agentbus node list` → shows every node with state (RUNNING/ERROR).
- [ ] `uv run agentbus node info chat_planner` → shows subscriptions, publications, queue depth.
- [ ] `uv run agentbus graph --format mermaid` → copyable mermaid.
- [ ] `uv run agentbus graph --format dot` → copyable Graphviz.
- [ ] `uv run --socket-path /tmp/nonexistent.sock topic list` → clear error, not a traceback.

---

## 16. Examples

Every example in `examples/` should run to completion without warnings:

- [ ] `uv run python examples/echo_agent/main.py`
- [ ] `uv run python examples/sensor_pipeline/main.py`
- [ ] `uv run python examples/tool_agent/main.py` (requires `ANTHROPIC_API_KEY`)
- [ ] `uv run python examples/bus_agent/main.py`
- [ ] `uv run python examples/writer_critic/main.py`
- [ ] `uv run python examples/swarm/main.py`

---

## 17. Smoke matrix (quick regression after any large change)

The 10-minute end-of-day version of this plan:

- [ ] `uv run pytest tests/ -q` → 473 pass
- [ ] `uv run ruff check .` → clean
- [ ] `uv run mypy agentbus` → clean (strict on public API)
- [ ] `uv run agentbus doctor` → ✓
- [ ] `uv run agentbus chat --headless` → send one prompt, get one reply
- [ ] `uv run agentbus chat` → TUI renders, toolbar readable, spinner fires, meter climbs

If all six boxes tick, the release surface is stable enough for a tag.
