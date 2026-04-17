"""Built-in chat tools: bash, file_read, file_write, code_exec."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agentbus.harness.providers import ToolSchema
from agentbus.message import Message
from agentbus.node import Node
from agentbus.schemas.common import ToolRequest
from agentbus.schemas.common import ToolResult as BusToolResult

# ---------------------------------------------------------------------------
# Tool schemas (LLM-facing declarations)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, ToolSchema] = {
    "bash": ToolSchema(
        name="bash",
        description=(
            "Execute a bash/shell command and return its output. "
            "Use for file listing, searching, running scripts, and system commands."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds (default: 30)",
                },
            },
            "required": ["command"],
        },
    ),
    "file_read": ToolSchema(
        name="file_read",
        description="Read a file and return its full contents.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read",
                },
            },
            "required": ["path"],
        },
    ),
    "file_write": ToolSchema(
        name="file_write",
        description="Write content to a file, creating parent directories as needed.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
    ),
    "code_exec": ToolSchema(
        name="code_exec",
        description="Execute Python code in a subprocess and return stdout/stderr.",
        input_schema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
            },
            "required": ["code"],
        },
    ),
}


# ---------------------------------------------------------------------------
# Tool handlers (pure async functions)
# ---------------------------------------------------------------------------


async def _run_bash(params: dict[str, Any]) -> str:
    command = params.get("command", "")
    timeout = float(params.get("timeout", 30.0))
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode(errors="replace")
        err_text = stderr.decode(errors="replace").strip()
        if err_text:
            output = output + f"\n[stderr]\n{err_text}"
        return output.strip() or "(no output)"
    except TimeoutError:
        return f"Error: command timed out after {timeout}s"
    except Exception as exc:
        return f"Error: {exc}"


async def _run_file_read(params: dict[str, Any]) -> str:
    path_str = params.get("path", "")
    try:
        return Path(path_str).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"Error: file not found: {path_str}"
    except PermissionError:
        return f"Error: permission denied: {path_str}"
    except Exception as exc:
        return f"Error reading {path_str}: {exc}"


async def _run_file_write(params: dict[str, Any]) -> str:
    path_str = params.get("path", "")
    content = params.get("content", "")
    try:
        p = Path(path_str)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path_str}"
    except PermissionError:
        return f"Error: permission denied: {path_str}"
    except Exception as exc:
        return f"Error writing {path_str}: {exc}"


async def _run_code_exec(params: dict[str, Any]) -> str:
    code = params.get("code", "")
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3",
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        output = stdout.decode(errors="replace")
        err_text = stderr.decode(errors="replace").strip()
        if err_text:
            output = output + f"\n[stderr]\n{err_text}"
        return output.strip() or "(no output)"
    except TimeoutError:
        return "Error: code execution timed out after 30s"
    except Exception as exc:
        return f"Error: {exc}"


TOOL_HANDLERS: dict[str, Any] = {
    "bash": _run_bash,
    "file_read": _run_file_read,
    "file_write": _run_file_write,
    "code_exec": _run_code_exec,
}


# ---------------------------------------------------------------------------
# ChatToolNode — bus node that executes tool calls
# ---------------------------------------------------------------------------


class ChatToolNode(Node):
    """Executes tool calls for chat mode via the bus.

    Subscribes to /tools/request, dispatches to the matching handler,
    and publishes the result to /tools/result with the correlation_id
    echoed back so PlannerNode's bus.request() future resolves.
    """

    name = "chat_tools"
    subscriptions = ["/tools/request"]
    publications = ["/tools/result"]

    def __init__(self, enabled_tools: list[str]) -> None:
        self._enabled = set(enabled_tools)
        self._bus = None

    async def on_init(self, bus) -> None:
        self._bus = bus

    async def on_message(self, msg: Message) -> None:
        request: ToolRequest = msg.payload
        tool = request.tool

        if tool not in self._enabled:
            output, error = None, f"Tool '{tool}' is not enabled in this session"
        elif tool in TOOL_HANDLERS:
            try:
                output = await TOOL_HANDLERS[tool](request.params)
                error = None
            except Exception as exc:
                output, error = None, str(exc)
        else:
            output, error = None, f"Unknown tool: {tool}"

        await self._bus.publish(
            "/tools/result",
            BusToolResult(tool_call_id=msg.id, output=output, error=error),
            correlation_id=msg.correlation_id,
        )
