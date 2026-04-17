"""Tests for chat tool permission policy and ChatToolNode gating."""

from __future__ import annotations

import asyncio

import pytest

from agentbus.bus import MessageBus
from agentbus.chat._permissions import (
    PermissionPolicy,
    ToolPermission,
    load_policy_from_dict,
)
from agentbus.chat._tools import ChatToolNode
from agentbus.message import Message
from agentbus.node import NodeState
from agentbus.schemas.common import ToolRequest
from agentbus.schemas.common import ToolResult as BusToolResult
from agentbus.topic import Topic

# ── Unit: policy.check() ──────────────────────────────────────────────────────


def test_default_policy_allows_everything():
    policy = PermissionPolicy()
    assert policy.check("bash", {"command": "ls"}).decision == "allow"
    assert policy.check("file_read", {"path": "/etc/passwd"}).decision == "allow"


def test_deny_mode_blocks_tool():
    policy = PermissionPolicy(tools={"bash": ToolPermission(mode="deny")})
    check = policy.check("bash", {"command": "ls"})
    assert check.decision == "deny"
    assert "denied by policy" in check.reason


def test_approval_required_mode_returns_approval_required():
    policy = PermissionPolicy(tools={"bash": ToolPermission(mode="approval_required")})
    check = policy.check("bash", {"command": "ls"})
    assert check.decision == "approval_required"


def test_bash_deny_command_prefix_blocks():
    policy = PermissionPolicy(
        tools={"bash": ToolPermission(mode="allow", deny_commands=["rm", "sudo"])}
    )
    assert policy.check("bash", {"command": "rm -rf /"}).decision == "deny"
    assert policy.check("bash", {"command": "sudo ls"}).decision == "deny"
    assert policy.check("bash", {"command": "ls"}).decision == "allow"


def test_bash_allow_commands_is_exclusive():
    """An allowlist, when set, denies anything not on it."""
    policy = PermissionPolicy(
        tools={"bash": ToolPermission(mode="allow", allow_commands=["ls", "cat"])}
    )
    assert policy.check("bash", {"command": "ls /tmp"}).decision == "allow"
    assert policy.check("bash", {"command": "cat x.txt"}).decision == "allow"
    assert policy.check("bash", {"command": "git status"}).decision == "deny"


def test_bash_deny_overrides_allowlist():
    policy = PermissionPolicy(
        tools={
            "bash": ToolPermission(
                mode="allow",
                allow_commands=["git"],
                deny_commands=["git push"],
            )
        }
    )
    assert policy.check("bash", {"command": "git push origin main"}).decision == "deny"
    assert policy.check("bash", {"command": "git status"}).decision == "allow"


def test_file_path_deny_root(tmp_path):
    forbidden = tmp_path / "secret"
    forbidden.mkdir()
    policy = PermissionPolicy(
        tools={"file_read": ToolPermission(mode="allow", deny_paths=[str(forbidden)])}
    )
    check = policy.check("file_read", {"path": str(forbidden / "a.txt")})
    assert check.decision == "deny"
    assert "deny root" in check.reason


def test_file_path_allowlist_blocks_escape(tmp_path):
    allowed = tmp_path / "sandbox"
    allowed.mkdir()
    policy = PermissionPolicy(
        tools={"file_write": ToolPermission(mode="allow", allow_paths=[str(allowed)])}
    )
    # Inside allowlist: ok.
    assert policy.check(
        "file_write", {"path": str(allowed / "out.txt")}
    ).decision == "allow"
    # Traversal attempt: resolved absolute path is outside the allowlist → deny.
    traversal = f"{allowed}/../escape.txt"
    assert policy.check("file_write", {"path": traversal}).decision == "deny"


def test_approval_not_triggered_when_deny_rule_matches():
    """A matched deny rule should short-circuit before the approval prompt."""
    policy = PermissionPolicy(
        tools={
            "bash": ToolPermission(
                mode="approval_required",
                deny_commands=["rm"],
            )
        }
    )
    assert policy.check("bash", {"command": "rm -rf /"}).decision == "deny"
    assert policy.check("bash", {"command": "ls"}).decision == "approval_required"


# ── YAML loader ──────────────────────────────────────────────────────────────


def test_load_policy_from_dict_minimal():
    policy = load_policy_from_dict(
        {
            "bash": {"mode": "approval_required"},
            "file_write": {"mode": "allow", "allow_paths": ["/tmp"]},
        }
    )
    assert policy.get("bash").mode == "approval_required"
    assert policy.get("file_write").mode == "allow"
    assert policy.get("file_write").allow_paths == ["/tmp"]
    # Unknown tool falls back to default (allow).
    assert policy.get("code_exec").mode == "allow"


def test_load_policy_from_dict_none_yields_empty_policy():
    policy = load_policy_from_dict(None)
    assert policy.get("bash").mode == "allow"


def test_load_policy_rejects_invalid_mode():
    with pytest.raises(ValueError, match="mode"):
        load_policy_from_dict({"bash": {"mode": "maybe"}})


def test_load_policy_rejects_non_mapping_entry():
    with pytest.raises(ValueError, match="mapping"):
        load_policy_from_dict({"bash": "allow"})


# ── ChatToolNode integration ────────────────────────────────────────────────


def _make_bus_with_tool_node(
    *,
    enabled_tools: list[str],
    policy: PermissionPolicy | None = None,
    approval_callback=None,
) -> tuple[MessageBus, ChatToolNode, asyncio.Queue]:
    bus = MessageBus(socket_path=None)
    bus.register_topic(Topic[ToolRequest]("/tools/request", retention=5))
    bus.register_topic(Topic[BusToolResult]("/tools/result", retention=5))

    tool_node = ChatToolNode(
        enabled_tools,
        permissions=policy,
        approval_callback=approval_callback,
    )
    bus.register_node(tool_node)

    # Sink to observe results.
    result_q: asyncio.Queue = asyncio.Queue()
    bus._topics["/tools/result"].add_subscriber("_sink_", result_q)
    return bus, tool_node, result_q


async def _dispatch(
    bus: MessageBus,
    tool_node: ChatToolNode,
    result_q: asyncio.Queue,
    tool: str,
    params: dict,
) -> BusToolResult:
    bus._nodes["chat_tools"].state = NodeState.RUNNING
    tool_node._bus = bus._handles["chat_tools"] if hasattr(bus, "_handles") else None

    # Simulate on_init wiring via the bus's init phase.
    await bus._init_phase()

    bus.publish("/tools/request", ToolRequest(tool=tool, params=params))
    await bus.spin_once(timeout=1.0)

    msg: Message = await asyncio.wait_for(result_q.get(), timeout=1.0)
    return msg.payload


async def test_tool_node_denies_blocked_command():
    policy = PermissionPolicy(
        tools={"bash": ToolPermission(mode="allow", deny_commands=["rm"])}
    )
    bus, tool_node, result_q = _make_bus_with_tool_node(
        enabled_tools=["bash"], policy=policy
    )
    result = await _dispatch(bus, tool_node, result_q, "bash", {"command": "rm -rf /tmp/x"})
    assert result.output is None
    assert result.error is not None
    assert "Permission denied" in result.error


async def test_tool_node_executes_allowed_command():
    bus, tool_node, result_q = _make_bus_with_tool_node(enabled_tools=["bash"])
    result = await _dispatch(bus, tool_node, result_q, "bash", {"command": "echo hi"})
    assert result.error is None
    assert "hi" in (result.output or "")


async def test_approval_required_without_callback_fails_closed():
    policy = PermissionPolicy(tools={"bash": ToolPermission(mode="approval_required")})
    bus, tool_node, result_q = _make_bus_with_tool_node(
        enabled_tools=["bash"], policy=policy, approval_callback=None
    )
    result = await _dispatch(bus, tool_node, result_q, "bash", {"command": "echo hi"})
    assert result.output is None
    assert result.error is not None
    assert "denied by user" in result.error


async def test_approval_required_with_approving_callback():
    policy = PermissionPolicy(tools={"bash": ToolPermission(mode="approval_required")})
    calls: list[tuple[str, dict, str]] = []

    async def approve(tool: str, params: dict, reason: str) -> bool:
        calls.append((tool, params, reason))
        return True

    bus, tool_node, result_q = _make_bus_with_tool_node(
        enabled_tools=["bash"], policy=policy, approval_callback=approve
    )
    result = await _dispatch(bus, tool_node, result_q, "bash", {"command": "echo ok"})
    assert result.error is None
    assert "ok" in (result.output or "")
    assert len(calls) == 1
    assert calls[0][0] == "bash"


async def test_approval_required_with_declining_callback():
    policy = PermissionPolicy(tools={"bash": ToolPermission(mode="approval_required")})

    async def decline(tool: str, params: dict, reason: str) -> bool:
        return False

    bus, tool_node, result_q = _make_bus_with_tool_node(
        enabled_tools=["bash"], policy=policy, approval_callback=decline
    )
    result = await _dispatch(bus, tool_node, result_q, "bash", {"command": "echo nope"})
    assert result.output is None
    assert "denied by user" in (result.error or "")


async def test_approval_callback_exception_fails_closed():
    policy = PermissionPolicy(tools={"bash": ToolPermission(mode="approval_required")})

    async def raises(tool: str, params: dict, reason: str) -> bool:
        raise RuntimeError("boom")

    bus, tool_node, result_q = _make_bus_with_tool_node(
        enabled_tools=["bash"], policy=policy, approval_callback=raises
    )
    result = await _dispatch(bus, tool_node, result_q, "bash", {"command": "echo x"})
    assert result.output is None
    assert "denied by user" in (result.error or "")
