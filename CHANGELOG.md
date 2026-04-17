# Changelog

All notable changes to AgentBus are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and AgentBus adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (Tier 3 — in progress)
- **Memory node.** New `agentbus.memory` module adds a `MemoryNode` that
  pairs inbound/outbound chat turns, embeds the combined text with a
  pluggable `EmbeddingProvider` (default: Ollama `/api/embed` with
  `nomic-embed-text`), and persists them to a local SQLite database
  (default `~/.agentbus/memory.db`) with struct-packed float32
  embeddings. Exposes a `memory_search` tool (registered with the
  planner automatically when memory is enabled) that embeds the query
  and ranks turns by pure-Python cosine similarity — no numpy, no
  vector-store dependency. Configure via `memory:` in `agentbus.yaml`:
  ```yaml
  memory:
    enabled: true
    provider: ollama
    model: nomic-embed-text
    base_url: http://localhost:11434
    db_path: ~/.agentbus/memory.db
  ```
  Lifecycle mirrors MCP: `open_memory_runtime()` probes the embedding
  provider before the bus starts so a missing Ollama surfaces at boot,
  not mid-conversation. Embedding failures on a single turn are logged
  and dropped (the turn is simply not stored). `MemoryNode`,
  `ChatToolNode`, and `MCPGatewayNode` all subscribe to
  `/tools/request` and silent-drop tools they don't own.
- **MCP gateway.** New `agentbus.mcp` module adds `MCPServerConfig`,
  `MCPRuntime`, and `MCPGatewayNode`. Each MCP server is spawned as a
  stdio subprocess via the official `mcp` Python SDK (optional extra,
  `uv sync --extra mcp`); its advertised tools are discovered at
  startup and registered with the planner under namespaced names
  (`mcp__<server>__<tool>`) so they can't collide with builtins. The
  gateway subscribes to `/tools/request`, silently drops anything it
  doesn't own so `ChatToolNode` and `MCPGatewayNode` compose cleanly,
  and publishes results on `/tools/result` with `CallToolResult.isError`
  mapped to the bus-facing `error` field. Configure via `mcp_servers:`
  in `agentbus.yaml`:
  ```yaml
  mcp_servers:
    - name: filesystem
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
  ```
  Lifecycle ownership is split: `open_mcp_runtime()` opens subprocesses
  inside the caller's task (the chat runner), and
  `await runtime.aclose()` runs in the same task on shutdown — this is
  required because the SDK uses anyio cancel scopes that must enter and
  exit in the same task.

### Added (Tier 2 — in progress)
- **Structured logging.** New `agentbus.logging_config` module with a
  `JSONFormatter`, a text formatter, and a `setup_logging()` entry point
  controllable via `--log-level` / `--log-format` CLI flags or
  `AGENTBUS_LOG_LEVEL` / `AGENTBUS_LOG_FORMAT` / `AGENTBUS_LOG_FILE`
  environment variables. Correlation IDs flow via a
  `contextvars.ContextVar` — the bus sets the ID around every
  `on_message` dispatch, so any `self.logger.info(...)` inside a handler
  is automatically tagged. Nodes now have a `self.logger` property that
  returns the child logger `agentbus.node.<name>`.
- **Graceful shutdown.** `MessageBus.spin()` now accepts `drain_timeout`
  (seconds to let node loops finish queued messages after shutdown is
  requested before force-cancel) and `install_signal_handlers` (wire
  SIGTERM/SIGINT to trigger cooperative exit, with a second signal
  escalating to immediate cancel). `agentbus launch` opts in by default
  (`drain_timeout=5.0`, `install_signal_handlers=True`); overridable via
  `bus.shutdown.*` keys in `agentbus.yaml`. Library embedders and the
  textual TUI keep the previous behaviour (no signal handlers, immediate
  cancel) by default.
- **Atomic session persistence.** `Session.save()` now writes via a
  sibling temp file + `fsync` + `os.replace`, so a SIGKILL mid-save
  leaves either the previous full JSON or the new one at the session
  path — never a truncated file.
- **`/trace` and `/usage` slash commands.** `/trace [cid|topic] [limit]`
  walks the bus message log and prints the causal chain for a
  correlation ID (directly, by prefix match, or by looking up the most
  recent correlated message on a given topic). `/usage` aggregates
  session tokens by conversation role (user / assistant / tool_result)
  alongside the active provider + model.
- **`agentbus daemon` subcommand.** New `agentbus.daemon` module adds
  pidfile-locked foreground execution (`daemon start`), `daemon stop`
  (SIGTERM + graceful wait), `daemon status`, and `daemon install
  {systemd,launchd}` to render a service-file template for the supplied
  config. The pidfile uses an `fcntl.flock` advisory lock so a second
  instance fails fast with exit code 2 rather than racing. Templates
  hard-code `Type=simple` / foreground `ProgramArguments` so
  systemd/launchd own lifecycle — combined with the Phase 2A graceful
  shutdown path, SIGTERM from the service manager triggers a drain +
  clean exit.
- **Tool permission policy.** New `permissions:` section in
  `agentbus.yaml` lets users set a per-tool mode (`allow`, `deny`,
  `approval_required`) plus optional allowlists/denylists:
  `deny_commands` / `allow_commands` (prefix match on the leading token
  of a bash command) and `deny_paths` / `allow_paths` (directory roots,
  expanded and resolved so `../` traversal cannot escape an allowlist).
  `approval_required` prompts the user via stdin in headless TTY mode
  and fails closed otherwise, so gated tools never silently run. The
  existing default (no `permissions:` section) remains fully
  backwards-compatible.

### Added
- **Tooling.** `ruff` (lint + format), `mypy` (strict on the public API),
  `pre-commit`, and `detect-secrets` are now first-class development
  dependencies. See `docs/production-plan.md` for the full Tier 1 plan.
- **GitHub Actions CI.** Lint, typecheck, and test matrix across Python 3.12
  and 3.13.
- **`agentbus doctor`.** Diagnostic subcommand that checks Python version,
  optional-dep availability, `~/.agentbus` writability, `agentbus.yaml`
  validity, socket reachability, and provider credentials. Exits non-zero on
  any failure so it's CI-friendly.
- **`agentbus --version`.** Prints the installed version from `pyproject.toml`
  via `importlib.metadata`. Exposed as `agentbus.__version__` on the Python
  API.
- **`Topic[T]` is now properly generic.** Adding `Generic[T]` enables type
  checkers to see `Topic[InboundChat]` as a parameterized type while keeping
  the runtime `__class_getitem__` subclass-factory behavior.

### Changed
- **`MessageBus.request()`** now re-raises `RequestTimeoutError` via
  `raise ... from None`, suppressing the chained `TimeoutError` traceback that
  was surfacing in user-visible errors.
- **Provider import errors** (anthropic/openai/httpx) raise `SystemExit`
  without a chained traceback, so missing optional extras produce a clean
  one-line message.
- **`pyyaml` is now a core dependency** (previously in the `cli`/`tui` extras).
  `agentbus chat` and `agentbus launch` both require YAML parsing at startup;
  the old silent fallback to `json.loads` produced a baffling
  `JSONDecodeError` when a user installed the base package and pointed at a
  YAML config.

## [0.1.0] - 2026-04-16

Initial prototype.

### Added
- ROS-inspired typed pub/sub message bus (`MessageBus`, `Topic[T]`, `Node`).
- Wildcard topic matching (`*`, `**`) and per-topic backpressure policies
  (`drop-oldest`, `drop-newest`).
- Built-in `/system/*` topics: lifecycle, heartbeat, backpressure, telemetry.
- Unix-socket introspection server and `agentbus topic/node/graph/launch`
  subcommands.
- LLM harness (`agentbus.harness`) with provider adapters for Anthropic,
  OpenAI, and Ollama; session persistence under `~/.agentbus/sessions/`.
- Interactive `agentbus chat` mode — headless, verbose, and textual-TUI I/O
  surfaces; `ChatPlannerNode`, `ChatToolNode`, and slash commands.
- YAML launcher (`agentbus launch agentbus.yaml`) and `GatewayNode` base
  class for external-channel bridges.

[Unreleased]: https://github.com/plarotta/agentbus/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/plarotta/agentbus/releases/tag/v0.1.0
