# AgentBus — Production Readiness Plan

Status: **Tiers 1 + 2 complete; Tier 3 shipped (MCP, memory, channels, swarm).** See `CHANGELOG.md` for the version-by-version ship log.

This plan turns AgentBus from an MVP into a production-grade system. It is organized in three tiers; each tier is independently shippable. References: [openclaw/openclaw](https://github.com/openclaw/openclaw) (engineering hygiene, daemon support, observability, permissions) and [plarotta/claude-code](https://github.com/plarotta/claude-code) (MCP, multi-agent orchestration, background memory).

## Tier 1 — Engineering hygiene

Low-risk, high-leverage. No architectural changes. Prereq for Tiers 2 and 3.

- **Lint + format + type-check.** `ruff` (lint + format) and `mypy --strict` on the public API. Wire into `pre-commit` so local commits are clean. Fix what surfaces; don't defer.
- **Version wiring.** Single source of truth in `pyproject.toml`. Expose `agentbus.__version__` via `importlib.metadata`. `agentbus --version` prints it.
- **`CHANGELOG.md`.** Keep-a-Changelog format, seeded with v0.1.0 covering MVP + chat mode.
- **GitHub Actions CI.** One workflow: lint → typecheck → test on Python 3.12 and 3.13, driven by `uv`. Cache the uv download.
- **Secret scanning.** `detect-secrets` with a baseline file and a `pre-commit` hook. Covers accidentally committed API keys (Anthropic / OpenAI / etc).
- **`agentbus doctor`.** Diagnostic subcommand. Checks: Python version, optional dep availability per configured provider, `~/.agentbus/sessions` writable, `agentbus.yaml` validates against `ChatConfig`, socket reachable (if a bus is running), provider creds in env. Exits non-zero on any failure so it's CI-friendly.
- **`agentbus setup` wizard. ✅ Shipped.** `agentbus.setup` — interactive full-config flow backed by `questionary` (`uv sync --extra tui`) with an ANSI theme and AgentBus banner. `Prompter` Protocol keeps the wizard test-driven: `QuestionaryPrompter` runs the TUI; `FakePrompter(answers)` lets 24 tests in `tests/test_setup.py` exercise every path without a TTY. Flow: existing-config detect (edit/overwrite/cancel) → provider+model → tools → memory → channels loop → atomic `.bak`-preserving write → doctor probe. `ChannelPlugin.interactive_setup(prompter, existing)` lets each plugin own its sub-flow while inheriting the wizard's styling; Slack and Telegram both ship themed flows. Exit codes: `0` wrote, `1` cancelled, `2` validation error.

### Tier 1 acceptance
- `uv run ruff check .` clean
- `uv run mypy agentbus` clean (strict on the public API)
- `uv run pytest` green on 3.12 + 3.13 in CI
- `uv run agentbus doctor` reports OK on a fresh clone after `uv sync --extra all`
- `agentbus --version` prints the version from `pyproject.toml`
- `pre-commit run --all-files` clean

## Tier 2 — Operational hardening

- **Structured logging.** JSON handler with correlation IDs, per-node child loggers. Log levels controllable via env + config.
- **`/trace <topic>` and `/usage`** slash commands in chat; taps `/system/telemetry` and per-node counters already on the bus.
- **Tool permissions model.** Per-tool policies: `allow` / `deny` / `approval_required`. Approval mode pauses tool dispatch and surfaces a prompt in the TUI / headless loop (mirrors openclaw's DM pairing-code policy).
- **Sandboxed bash / code_exec. ✅ Shipped.** `agentbus.chat._sandbox` — `SubprocessSandbox` (default) applies `RLIMIT_CPU` / `RLIMIT_AS` via `preexec_fn`, scrubs env to a 5-var allowlist (+ `env_passthrough`), runs each command in a per-invocation tempdir, and kills the whole process group on timeout via `os.killpg`. Output is truncated at `max_output_bytes` (default 256 KiB) so a runaway process can't blow the LLM's context window. `DockerSandbox` is opt-in (`sandbox.backend: docker`) and wraps `docker run --rm --read-only --memory --cpus --network=none` with a single `/workspace` bind mount. Sandbox is **on by default** — omitting the block still enforces limits. Permission policy runs *above* the sandbox, so denied commands short-circuit before a child is spawned.
- **Daemon support.** `launchd` plist template (macOS) and `systemd` unit template (Linux). `agentbus daemon install/uninstall/status` subcommands.
- **Graceful shutdown.** SIGTERM handler drains in-flight messages up to a timeout, flushes session to disk, closes socket, then exits. Crash-safe session persistence via atomic write + rename.

## Tier 3 — Feature parity with references

- **MCP gateway. ✅ Shipped.** `agentbus.mcp` spawns configured MCP stdio servers via the official `mcp` Python SDK (`uv sync --extra mcp`), discovers advertised tools, and registers them with the planner under `mcp__<server>__<tool>` names. `MCPGatewayNode` subscribes to `/tools/request` using the silent-drop pattern so it composes with `ChatToolNode`. Lifecycle ownership is split: `open_mcp_runtime()` and `runtime.aclose()` must run in the same task because the SDK's anyio cancel scopes cannot cross task boundaries.
- **Multi-agent orchestration. ✅ Shipped (hub-and-spoke).** `agentbus.swarm` — `register_swarm(bus, specs, config)` returns a `dispatch_subagent` `ToolSchema` the coordinator planner plugs in via `extra_tools=[...]`. Each `SubAgentSpec` gets namespaced topics `/swarm/<name>/inbound` + `/swarm/<name>/outbound`; `SwarmAgentNode` builds a fresh `Harness` + `Session` per dispatch (stateless across calls, matching claude-code's Task tool). Sub-agents never talk to each other; all handoffs route through the coordinator. Peer-to-peer / persistent sessions / nested swarms deferred on purpose.
- **Background memory consolidation. ✅ Shipped.** `agentbus.memory` pairs inbound/outbound chat turns, embeds them via a pluggable `EmbeddingProvider` (default: Ollama `nomic-embed-text`), and persists struct-packed float32 vectors to SQLite (`~/.agentbus/memory.db` by default). Exposes a `memory_search` tool with pure-Python cosine ranking — no numpy, no vector-store dependency. Fail-closed lifecycle: startup embedding probe failure → warn and continue without memory; per-turn embedding failure → drop; search failure → `ToolResult.error`.
- **Multi-channel gateways. ✅ Shipped + hardened.** `agentbus.channels` ports a plugin-per-channel architecture from openclaw in trimmed form. Two gateways ship in-tree: `channels/slack` (Socket Mode via `slack-bolt`, `uv sync --extra slack`) and `channels/telegram` (httpx long-poll, `uv sync --extra telegram`). Each implements `ChannelPlugin[ConfigT]` — `name`, `ConfigModel`, `setup_wizard`, `create_gateway`, optional `probe`. `OutboundChat.channel` + `metadata` route + thread replies per-gateway; allowlists filter before messages reach `/inbound`; circuit breaker parks a dead token after 5 consecutive failures. `agentbus channels list|setup` + `/system/channels` status topic round out the surface. Reliability primitives shared across gateways:
  - **Exponential backoff with jitter** (`channels/reconnect.py`) replaces the hard-coded 5-second retry. Defaults: initial 2s, max 30s, factor 1.8, jitter 25%.
  - **Non-recoverable auth-error short-circuit** — per-channel regex (`SLACK_NON_RECOVERABLE_RE`, `TELEGRAM_NON_RECOVERABLE_RE`) halts the retry loop immediately on `token_revoked`, `invalid_auth`, HTTP 401, etc. — no wasted retries against a dead token.
  - **Outbound chunking** (`channels/chunking.py`) — splits long replies at paragraph/line/space boundaries under the channel's char limit (`SLACK_TEXT_LIMIT=8000`, `TELEGRAM_TEXT_LIMIT=4096`). First Telegram chunk keeps `reply_to_message_id`; follow-ups are plain.
  - **Inbound dedup cache** (`channels/dedup.py`) — bounded LRU keyed on natural idempotence key (Slack `ts`, Telegram `update_id`) survives Socket Mode redeliveries and offset races.
  - **Slack self-echo filter** — outbound `chat.postMessage` responses' `ts` values are cached so our own replies aren't treated as inbound events in public channels.
  - **Stall watchdog** (`channels/watchdog.py`) — if the Telegram poll loop goes idle beyond `3 × long_poll_timeout_s + 30s` (no successful fetch), the listener is cancelled so the reconnect loop re-enters cleanly. Slack Socket Mode relies on slack-bolt's built-in keepalive.
  - **Per-channel doctor probe** — `SlackPlugin.probe` calls `auth.test`; `TelegramPlugin.probe` calls `getMe`. `agentbus doctor` runs the probe for every configured channel and surfaces `ok / warn / fail` alongside the other diagnostics.

## Non-goals

- Distributed / multi-host message routing. AgentBus is local-first by design (PRD §2); cross-host is v2.
- Replacing `asyncio` with threads or multiprocessing.
- Windows-first support. Unix domain sockets are intentional; Windows is best-effort.
- A web UI. The TUI and CLI are the supported surfaces.
