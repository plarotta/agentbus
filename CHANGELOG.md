# Changelog

All notable changes to AgentBus are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and AgentBus adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
