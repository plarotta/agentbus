"""Tool permission policy for `agentbus chat`.

Three modes per tool:
  - ``allow``              — always execute (default when unspecified).
  - ``deny``               — never execute; return a permission-denied error.
  - ``approval_required``  — prompt the user (via an injected callback).
                             If no callback is available (TUI, tests), fail closed.

Optional rules add fine-grained control on top of the mode. They are
evaluated before ``approval_required`` prompts the user, so a matched
deny rule short-circuits straight to denial without asking.

  * ``bash``: ``deny_commands`` and ``allow_commands`` prefix-match the
    leading token of the ``command`` parameter. An allowlist, when set,
    is exclusive — anything not on it is denied.
  * ``file_read`` / ``file_write``: ``deny_paths`` / ``allow_paths`` are
    directory roots. Paths are expanded (``~``) and resolved to absolute
    form before comparison, so ``../`` traversal cannot escape an
    allowlist.
  * ``code_exec``: rules have no parameter hook; only ``mode`` applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Mode = Literal["allow", "deny", "approval_required"]

_VALID_MODES: frozenset[str] = frozenset({"allow", "deny", "approval_required"})


@dataclass(frozen=True)
class PermissionCheck:
    """Outcome of a permission check for a single tool invocation."""

    decision: Literal["allow", "deny", "approval_required"]
    reason: str = ""


@dataclass
class ToolPermission:
    """Per-tool permission entry loaded from ``agentbus.yaml``."""

    mode: Mode = "allow"
    allow_commands: list[str] = field(default_factory=list)
    deny_commands: list[str] = field(default_factory=list)
    allow_paths: list[str] = field(default_factory=list)
    deny_paths: list[str] = field(default_factory=list)


@dataclass
class PermissionPolicy:
    """Collection of per-tool permissions with a default for unknown tools."""

    tools: dict[str, ToolPermission] = field(default_factory=dict)
    default: ToolPermission = field(default_factory=ToolPermission)

    def get(self, tool: str) -> ToolPermission:
        return self.tools.get(tool, self.default)

    def check(self, tool: str, params: dict[str, Any]) -> PermissionCheck:
        perm = self.get(tool)

        rule_check = _evaluate_rules(tool, params, perm)
        if rule_check is not None:
            return rule_check

        if perm.mode == "deny":
            return PermissionCheck("deny", f"tool {tool!r} is denied by policy")
        if perm.mode == "approval_required":
            return PermissionCheck(
                "approval_required", f"tool {tool!r} requires user approval"
            )
        return PermissionCheck("allow")


def _evaluate_rules(
    tool: str, params: dict[str, Any], perm: ToolPermission
) -> PermissionCheck | None:
    """Evaluate tool-specific deny/allow rules. Returns None if no rule fires."""
    if tool == "bash":
        command = str(params.get("command", ""))
        leading = command.strip().split(maxsplit=1)
        head = leading[0] if leading else ""

        for prefix in perm.deny_commands:
            if head == prefix or command.strip().startswith(prefix):
                return PermissionCheck("deny", f"bash command blocked by deny rule: {prefix!r}")

        if perm.allow_commands:
            matched = any(
                head == prefix or command.strip().startswith(prefix)
                for prefix in perm.allow_commands
            )
            if not matched:
                return PermissionCheck(
                    "deny",
                    f"bash command {head!r} not in allowlist {perm.allow_commands!r}",
                )

    if tool in ("file_read", "file_write"):
        raw_path = str(params.get("path", ""))
        if raw_path:
            target = _resolve(raw_path)
            for deny_root in perm.deny_paths:
                if _is_within(target, _resolve(deny_root)):
                    return PermissionCheck(
                        "deny", f"{tool!r}: path {raw_path!r} is under deny root {deny_root!r}"
                    )
            if perm.allow_paths:
                allowed = any(
                    _is_within(target, _resolve(root)) for root in perm.allow_paths
                )
                if not allowed:
                    return PermissionCheck(
                        "deny",
                        f"{tool!r}: path {raw_path!r} is outside allowlist {perm.allow_paths!r}",
                    )
    return None


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def load_policy_from_dict(data: dict[str, Any] | None) -> PermissionPolicy:
    """Parse the ``permissions:`` section of ``agentbus.yaml``.

    Shape:
        permissions:
          bash:
            mode: approval_required
            deny_commands: ["rm", "sudo"]
          file_write:
            mode: allow
            allow_paths: ["/tmp"]
    """
    if not data:
        return PermissionPolicy()

    tools: dict[str, ToolPermission] = {}
    for tool_name, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"permissions.{tool_name}: expected a mapping, got {type(entry).__name__}"
            )
        mode = entry.get("mode", "allow")
        if mode not in _VALID_MODES:
            raise ValueError(
                f"permissions.{tool_name}.mode: must be one of {sorted(_VALID_MODES)}, "
                f"got {mode!r}"
            )
        tools[tool_name] = ToolPermission(
            mode=mode,
            allow_commands=list(entry.get("allow_commands", [])),
            deny_commands=list(entry.get("deny_commands", [])),
            allow_paths=list(entry.get("allow_paths", [])),
            deny_paths=list(entry.get("deny_paths", [])),
        )
    return PermissionPolicy(tools=tools)


__all__ = [
    "Mode",
    "PermissionCheck",
    "PermissionPolicy",
    "ToolPermission",
    "load_policy_from_dict",
]
