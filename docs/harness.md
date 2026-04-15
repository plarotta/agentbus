# Harness Layer Reference

The harness is the LLM agent loop. It is a standalone component — it has
**zero imports from `agentbus.bus`, `agentbus.topic`, or `agentbus.node`**.
The only connection to the bus is the `tool_executor` callback injected at
construction time.

```
agentbus/harness/
├── loop.py         # Harness, HarnessDeps, ProductionDeps
├── session.py      # Session persistence and forking
├── compaction.py   # MicroCompact, AutoCompact, context window management
├── extensions.py   # Extension base class and hook runners
└── providers/
    ├── __init__.py # Chunk, ToolSchema, SystemPrompt, Provider protocol
    ├── anthropic.py
    ├── openai.py
    └── ollama.py
```

---

## Harness

```python
from agentbus.harness import Harness
```

### Constructor

```python
Harness(
    *,
    provider: Provider | None = None,
    tool_executor: Callable[[ToolCall], Awaitable[ToolResult] | ToolResult],
    tools: list[ToolSchema] | None = None,
    session: Session | None = None,
    extensions: Sequence[Extension] | None = None,
    deps: HarnessDeps | None = None,
    max_iterations: int = 25,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    stop: list[str] | None = None,
)
```

Either `provider` or `deps` must be provided. `deps` is for testing only —
pass a `provider` in production.

| Parameter | Description |
|-----------|-------------|
| `provider` | LLM provider instance. Mutually exclusive with `deps`. |
| `tool_executor` | Async or sync callable: `(ToolCall) -> ToolResult`. Called once per tool invocation. |
| `tools` | Tool declarations sent to the LLM. Empty list = no tools. |
| `session` | Session instance for conversation history. Defaults to a new `Session()`. |
| `extensions` | List of `Extension` instances applied to each pipeline stage. |
| `deps` | Injectable deps for testing (`FakeDeps` pattern). |
| `max_iterations` | Maximum tool-call iterations before forcing a text response. Default: 25. |
| `temperature` | LLM sampling temperature. Default: 0.0 (deterministic). |
| `max_tokens` | Override the provider's default max output tokens. |
| `stop` | List of stop sequences. |

### `run()`

```python
async def run(self, user_input: str) -> str
```

Append `user_input` to the session, run the agent loop, and return the final
text response. The session is saved to disk on each successful response.

**Agent loop behavior:**

1. Apply `on_context` extensions to session turns
2. Run `_maybe_compact()` if context window pressure is detected
3. Apply `on_before_llm` extensions
4. Call provider, accumulate streaming chunks
5. If the response contains tool calls:
   a. Append the assistant turn (with `tool_calls`) to the session
   b. For each tool call: apply `on_tool_call`, execute via `tool_executor`,
      apply `on_tool_result`, append `tool_result` turn, run `microcompact`
   c. Repeat from step 1 (next iteration)
6. Otherwise: apply `on_response`, append final assistant turn, save session,
   return text

If `max_iterations` is reached, a final provider call is made with `tools=[]`
to force a text response.

On exception: `on_error` extensions are called. If one returns a fallback
string, that is returned and the session is saved. Otherwise the exception
re-raises.

### `context_window`

```python
@property
def context_window(self) -> int
```

Returns the provider's `context_window`, or `128_000` if no provider is set.
Used by `_maybe_compact()` to determine when to compact.

---

## Session

```python
from agentbus.harness import Session
```

### Constructor

```python
Session(
    session_id: str | None = None,
    *,
    turns: list[ConversationTurn] | None = None,
    root_dir: Path | str | None = None,
    file_path: Path | str | None = None,
)
```

| Parameter | Description |
|-----------|-------------|
| `session_id` | Unique ID. Defaults to a new UUID4. |
| `turns` | Pre-populate with existing turns. |
| `root_dir` | Base directory. Defaults to `~/.agentbus/sessions`. |
| `file_path` | Override the computed file path entirely. |

**Default file layout:**

```
~/.agentbus/sessions/
└── {session_id}/
    ├── main.json
    ├── branch_1.json
    └── branch_2.json
```

### Methods

```python
session.append(turn: ConversationTurn) -> None
session.total_tokens() -> int        # sum of token_count across all turns
session.save() -> None               # write to file_path as JSON
```

**JSON format:**

```json
{
  "session_id": "abc123",
  "file": "main.json",
  "turns": [
    {
      "role": "user",
      "content": "hello",
      "tool_calls": null,
      "tool_call_id": null,
      "token_count": 1,
      "timestamp": "2026-01-01T00:00:00Z"
    }
  ]
}
```

### `Session.load()`

```python
@classmethod
def load(
    cls,
    session_id: str,
    *,
    root_dir: Path | str | None = None,
    file_name: str = "main.json",
) -> Session
```

Load a session from disk. Pass `file_name="branch_1.json"` to load a fork.

```python
session = Session.load("abc123")
session = Session.load("abc123", root_dir="/custom/path")
session = Session.load("abc123", file_name="branch_2.json")
```

### `Session.fork()`

```python
def fork(self, from_turn_index: int) -> Session
```

Create a branch from turn index `from_turn_index` (inclusive). The branch
contains turns `[0, from_turn_index]`. Saves to `branch_N.json` where N
auto-increments. The parent session is **not mutated**.

```python
branch = session.fork(4)  # turns 0-4, saved to branch_1.json
branch.file_path.name     # "branch_1.json"
len(branch.turns)         # 5
```

---

## Providers

All providers satisfy the `Provider` protocol:

```python
class Provider(Protocol):
    async def complete(
        self,
        messages: list[Any],
        tools: list[ToolSchema],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        signal: Any | None = None,
    ) -> AsyncIterator[Chunk]: ...

    @property
    def context_window(self) -> int: ...

    def count_tokens(self, messages: list[Any]) -> int: ...
```

### AnthropicProvider

```python
from agentbus.harness.providers.anthropic import AnthropicProvider
```

```python
AnthropicProvider(
    model: str,
    *,
    api_key: str | None = None,
    context_window: int = 200_000,
    system_prompt: SystemPrompt | None = None,
)
```

| Parameter | Description |
|-----------|-------------|
| `model` | Model ID, e.g. `"claude-haiku-4-5-20251001"`, `"claude-opus-4-6"` |
| `api_key` | API key. Defaults to `ANTHROPIC_API_KEY` env var. |
| `context_window` | Override context window size in tokens. |
| `system_prompt` | `SystemPrompt` with optional prompt caching. |

Requires the `anthropic` extra: `uv sync --extra anthropic`.

Message format translation:
- Assistant turns with tool calls → Anthropic content blocks of type `"tool_use"`
- Tool result turns → `"user"` role with `"tool_result"` content blocks
  (consecutive results are grouped into one `"user"` message)

### OpenAIProvider

```python
from agentbus.harness.providers.openai import OpenAIProvider
```

```python
OpenAIProvider(
    model: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    context_window: int = 128_000,
)
```

| Parameter | Description |
|-----------|-------------|
| `model` | Model ID, e.g. `"gpt-4o"`, `"gpt-4o-mini"` |
| `api_key` | API key. Defaults to `OPENAI_API_KEY` env var. |
| `base_url` | Override API base URL for compatible APIs (Azure, Together AI, etc.) |
| `context_window` | Override context window size. |

Requires the `openai` extra: `uv sync --extra openai`.

Compatible with any OpenAI-format API via `base_url`. Tool results are
formatted as `role: "tool"` messages.

### OllamaProvider

```python
from agentbus.harness.providers.ollama import OllamaProvider
```

```python
OllamaProvider(
    model: str,
    *,
    base_url: str = "http://localhost:11434",
    context_window: int = 32_768,
)
```

Requires the `ollama` extra (`httpx`): `uv sync --extra ollama`.

Uses the Ollama HTTP API at `/api/chat`. No API key needed.

---

## SystemPrompt

```python
from agentbus.harness.providers import SystemPrompt
```

```python
SystemPrompt(
    static_prefix: str,
    dynamic_suffix: str = "",
)
```

`static_prefix` is the stable part of the system prompt — it is sent with
Anthropic's `cache_control: ephemeral` header for prompt caching. The cache
break is between `static_prefix` and `dynamic_suffix`.

```python
prompt = SystemPrompt(
    static_prefix="You are a coding assistant. ...",  # cached
    dynamic_suffix=f"Today's date: {date.today()}",   # changes each request
)
```

**`render()`** returns the list of Anthropic content blocks. If
`dynamic_suffix` is empty, only the static block is returned (Anthropic
rejects empty content blocks).

**`render_plain()`** returns a plain string, used by providers that don't
support block-format system prompts.

---

## ToolSchema

```python
from agentbus.harness.providers import ToolSchema
```

```python
@dataclass
class ToolSchema:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
```

The `input_schema` follows JSON Schema format:

```python
ToolSchema(
    name="search",
    description="Search the web for information.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
            "max_results": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
)
```

An empty `input_schema` (default) means the tool takes no arguments.

---

## Extensions

```python
from agentbus.harness import Extension
```

`Extension` is a base class with passthrough implementations of every hook.
Subclass it and override only the hooks you need:

```python
class LoggingExtension(Extension):
    def on_tool_call(self, tool_call: ToolCall) -> ToolCall | None:
        print(f"[tool] {tool_call.name}({tool_call.arguments})")
        return tool_call   # returning None would skip execution

    def on_response(self, response: str) -> str:
        print(f"[response] {len(response)} chars")
        return response
```

Pass extensions to `Harness`:

```python
harness = Harness(
    provider=provider,
    tool_executor=executor,
    tools=tools,
    extensions=[LoggingExtension(), RedactSecretsExtension()],
)
```

### Hook reference

| Hook | Signature | Called when |
|------|-----------|-------------|
| `on_context` | `(messages) → messages` | Before each LLM call, with the full session |
| `on_before_llm` | `(messages, tools) → (messages, tools)` | Immediately before calling the provider |
| `on_tool_call` | `(ToolCall) → ToolCall \| None` | Before executing each tool call. Return `None` to skip the call entirely. |
| `on_tool_result` | `(ToolCall, ToolResult) → ToolResult` | After tool execution, before appending to session |
| `on_before_compact` | `(messages) → messages` | Before the compaction check |
| `on_response` | `(str) → str` | Before the final text response is returned |
| `on_error` | `(Exception) → str \| None` | On unhandled exception. Return a fallback string or `None` to re-raise. |

**Hook execution order:** extensions are applied in list order. Each hook
receives the output of the previous extension. `on_tool_call` short-circuits if
any extension returns `None`. `on_error` returns the first non-`None` fallback.

### Common extension patterns

**Filter tool calls:**

```python
class SafetyExtension(Extension):
    BLOCKED_TOOLS = {"shell", "exec"}

    def on_tool_call(self, tool_call: ToolCall) -> ToolCall | None:
        if tool_call.name in self.BLOCKED_TOOLS:
            return None  # skip execution
        return tool_call
```

**Inject dynamic context:**

```python
class ContextExtension(Extension):
    def __init__(self, system_context: str):
        self._ctx = system_context

    def on_context(self, messages):
        ctx_turn = ConversationTurn(
            role="user",
            content=f"[context] {self._ctx}",
            token_count=estimate_tokens(self._ctx),
        )
        return [ctx_turn] + list(messages)
```

**Redact tool outputs:**

```python
class RedactExtension(Extension):
    def on_tool_result(self, call: ToolCall, result: ToolResult) -> ToolResult:
        output = result.output or ""
        output = re.sub(r"\b\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}\b", "****", output)
        return result.model_copy(update={"output": output})
```

---

## Context compaction

The harness manages context window pressure automatically. When the total token
count of the session exceeds `context_window - AUTOCOMPACT_BUFFER_TOKENS`
(13,000 tokens), autocompaction triggers.

### Constants

```python
from agentbus.harness.compaction import (
    AUTOCOMPACT_BUFFER_TOKENS,   # 13_000 — headroom kept for new output
    MAX_TOOL_OUTPUT_TOKENS,      # 4_000  — microcompact threshold per tool output
    TRUNCATED_TOOL_OUTPUT_TOKENS,# 64     — microcompact truncation target
    MAX_CONSECUTIVE_COMPACT_FAILURES, # 3 — circuit breaker threshold
)
```

### MicroCompact

Applied after every tool result. Truncates tool outputs that exceed
`MAX_TOOL_OUTPUT_TOKENS` tokens to `TRUNCATED_TOOL_OUTPUT_TOKENS` tokens,
replacing the content with `[tool output truncated: N tokens -> 64 tokens]`.

This is stateless and cheap — it runs synchronously without any LLM calls.

### AutoCompact

Triggered when the total context exceeds the window. Uses the provider to
summarize all but the most recent `recent_turns` (default: 4) turns, then
replaces history with a single summary turn followed by the recent turns.

```python
class CompactResult:
    messages: list[ConversationTurn]  # new (shorter) history
    compacted: bool                    # False if compaction was skipped/failed
    summary: str | None               # the summary text
```

A `CircuitBreaker` with `max_failures=3` guards `AutoCompact`. If compaction
fails three times in a row, the breaker opens and subsequent compaction
attempts return `compacted=False` immediately.

### Token estimation

```python
from agentbus.harness.compaction import estimate_tokens
estimate_tokens("hello world")  # → 2
```

Uses `max(1, len(text) // 4)` — a fast heuristic. Not exact, but consistent.

---

## HarnessDeps (testing interface)

`HarnessDeps` is a Protocol that replaces the provider and compaction
infrastructure for tests:

```python
class HarnessDeps(Protocol):
    def call_provider(self, messages, tools, **kwargs) -> AsyncIterator[Chunk] | Awaitable[...]
    def microcompact(self, messages) -> list[ConversationTurn] | Awaitable[...]
    def autocompact(self, messages) -> CompactResult | Awaitable[...]
    def uuid(self) -> str
```

**FakeDeps pattern:**

```python
class FakeDeps:
    def __init__(self, turns: list[list[Chunk]]) -> None:
        self.turns = list(turns)

    async def call_provider(self, messages, tools, **kwargs):
        chunks = self.turns.pop(0)
        async def _stream():
            for chunk in chunks:
                yield chunk
        return _stream()

    async def microcompact(self, messages):
        return messages

    async def autocompact(self, messages):
        return CompactResult(messages=messages, compacted=False)

    def uuid(self):
        return "test-session"

# In tests:
deps = FakeDeps([[Chunk(text="hello")]])
harness = Harness(deps=deps, tool_executor=executor, tools=[])
result = await harness.run("say hello")
assert result == "hello"
```

`Chunk` fields: `text`, `tool_call_id`, `tool_name`, `tool_arguments`.
A `Chunk` with `tool_call_id` signals a tool invocation. Multiple chunks with
the same `tool_call_id` are accumulated (streaming tool argument fragments).
