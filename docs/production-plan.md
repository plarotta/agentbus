# AgentBus — Production Readiness Plan

Status: **Tier 1 in progress.** See `progress.md` for MVP status and `plan.md` for the original implementation spec.

This plan turns AgentBus from an MVP into a production-grade system. It is organized in three tiers; each tier is independently shippable. References: [openclaw/openclaw](https://github.com/openclaw/openclaw) (engineering hygiene, daemon support, observability, permissions) and [plarotta/claude-code](https://github.com/plarotta/claude-code) (MCP, multi-agent orchestration, background memory).

## Tier 1 — Engineering hygiene

Low-risk, high-leverage. No architectural changes. Prereq for Tiers 2 and 3.

- **Lint + format + type-check.** `ruff` (lint + format) and `mypy --strict` on the public API. Wire into `pre-commit` so local commits are clean. Fix what surfaces; don't defer.
- **Version wiring.** Single source of truth in `pyproject.toml`. Expose `agentbus.__version__` via `importlib.metadata`. `agentbus --version` prints it.
- **`CHANGELOG.md`.** Keep-a-Changelog format, seeded with v0.1.0 covering MVP + chat mode.
- **GitHub Actions CI.** One workflow: lint → typecheck → test on Python 3.12 and 3.13, driven by `uv`. Cache the uv download.
- **Secret scanning.** `detect-secrets` with a baseline file and a `pre-commit` hook. Covers accidentally committed API keys (Anthropic / OpenAI / etc).
- **`agentbus doctor`.** Diagnostic subcommand. Checks: Python version, optional dep availability per configured provider, `~/.agentbus/sessions` writable, `agentbus.yaml` validates against `ChatConfig`, socket reachable (if a bus is running), provider creds in env. Exits non-zero on any failure so it's CI-friendly.

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
- **Sandboxed bash / code_exec.** Default: `subprocess` with rlimits, CPU/memory caps, and a writable-path allowlist. Optional Docker backend behind a `sandbox: docker` flag in `agentbus.yaml`.
- **Daemon support.** `launchd` plist template (macOS) and `systemd` unit template (Linux). `agentbus daemon install/uninstall/status` subcommands.
- **Graceful shutdown.** SIGTERM handler drains in-flight messages up to a timeout, flushes session to disk, closes socket, then exits. Crash-safe session persistence via atomic write + rename.

## Tier 3 — Feature parity with references

- **MCP gateway.** A `GatewayNode` subclass that bridges MCP tools to `/tools/request` / `/tools/result`. Enables any MCP server as a tool source.
- **Multi-agent orchestration.** Example + reusable pattern: a `planner` node spawns sub-agents as additional `ChatPlannerNode` instances on isolated topic namespaces, with a router node coordinating handoffs. Inspired by claude-code's "Swarm".
- **Background memory consolidation.** A `MemoryNode` that subscribes to `/outbound` and conversation turns, periodically summarizes, writes to a local vector store, and surfaces retrieval via a `memory_search` tool. Inspired by claude-code's "Dream".
- **Multi-channel gateways.** `SlackGatewayNode`, `TelegramGatewayNode` — both subclass `GatewayNode` and translate external messages ↔ `/inbound` / `/outbound`. Inspired by openclaw's multi-platform support.

## Non-goals

- Distributed / multi-host message routing. AgentBus is local-first by design (PRD §2); cross-host is v2.
- Replacing `asyncio` with threads or multiprocessing.
- Windows-first support. Unix domain sockets are intentional; Windows is best-effort.
- A web UI. The TUI and CLI are the supported surfaces.
