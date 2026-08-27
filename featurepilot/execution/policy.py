"""Deterministic rules for deciding whether a tool request is in Plan scope."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from featurepilot.engine.permissions import (
    PermissionAction,
    PermissionDecision,
    PermissionEffect,
)

from .context import ExecutionContext

ToolEffect = PermissionEffect


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A policy result, including normalized arguments for an allowed request."""

    action: PermissionAction
    reason: str
    effect: ToolEffect
    arguments: dict[str, Any] = field(default_factory=dict)
    validation_command: tuple[str, ...] | None = None

    @property
    def allowed(self) -> bool:
        return self.action is PermissionAction.ALLOW

    def to_permission_decision(self) -> PermissionDecision:
        """Expose the Managed result through the reusable C3 protocol."""

        return PermissionDecision(self.action, self.reason)


class ToolPolicy:
    """Apply FeaturePilot's first-version workspace and Plan restrictions."""

    _EFFECTS = {
        "read_file": ToolEffect.READ,
        "glob": ToolEffect.READ,
        "grep": ToolEffect.READ,
        "now": ToolEffect.READ,
        "edit_file": ToolEffect.WRITE,
        "write_file": ToolEffect.WRITE,
        "bash": ToolEffect.EXECUTE,
        "fetch_url": ToolEffect.NETWORK,
        "agent": ToolEffect.DELEGATE,
    }

    def decide(self, tool_name: str, arguments: dict[str, Any], context: ExecutionContext) -> PolicyDecision:
        """Return an allow/deny decision without executing the requested tool."""

        effect = self._EFFECTS.get(tool_name, ToolEffect.UNKNOWN)
        if tool_name in {"fetch_url", "agent"}:
            return self._deny(effect, f"{tool_name} is disabled for FeaturePilot runs")
        if tool_name == "now":
            return self._allow(effect, arguments, "Time lookup is allowed")
        if tool_name == "read_file":
            return self._read_file(arguments, context)
        if tool_name in {"glob", "grep"}:
            return self._search(tool_name, arguments, context)
        if tool_name == "edit_file":
            return self._edit(arguments, context)
        if tool_name == "write_file":
            return self._write(arguments, context)
        if tool_name == "bash":
            return self._validation_command(arguments, context)
        return self._deny(effect, f"Tool {tool_name!r} is not available in a FeaturePilot run")

    @staticmethod
    def _allow(effect: ToolEffect, arguments: dict[str, Any], reason: str) -> PolicyDecision:
        return PolicyDecision(PermissionAction.ALLOW, reason, effect, dict(arguments))

    @staticmethod
    def _deny(effect: ToolEffect, reason: str) -> PolicyDecision:
        return PolicyDecision(PermissionAction.DENY, reason, effect)

    def _read_file(self, arguments: dict[str, Any], context: ExecutionContext) -> PolicyDecision:
        path = self._path_argument("read_file", arguments, "file_path", context)
        if isinstance(path, PolicyDecision):
            return path
        normalized = dict(arguments)
        normalized["file_path"] = str(path)
        return self._allow(ToolEffect.READ, normalized, "Read path is inside the Workspace")

    def _search(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> PolicyDecision:
        path = self._path_argument(tool_name, arguments, "path", context, default=".")
        if isinstance(path, PolicyDecision):
            return path
        pattern_names = ("pattern",) if tool_name == "glob" else ("include",)
        for pattern_name in pattern_names:
            pattern = arguments.get(pattern_name)
            if pattern is None and pattern_name == "include":
                continue
            pattern_error = self._validate_search_pattern(tool_name, pattern_name, pattern)
            if pattern_error:
                return self._deny(ToolEffect.READ, pattern_error)
        normalized = dict(arguments)
        normalized["path"] = str(path)
        return self._allow(ToolEffect.READ, normalized, "Search path is inside the Workspace")

    @staticmethod
    def _validate_search_pattern(tool_name: str, argument_name: str, value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return f"{tool_name} requires a non-empty {argument_name}"
        posix_pattern = PurePosixPath(value)
        windows_pattern = PureWindowsPath(value)
        if posix_pattern.is_absolute() or windows_pattern.is_absolute() or windows_pattern.drive:
            return f"{tool_name} {argument_name} must be relative to its Workspace search path"
        parts = value.replace("\\", "/").split("/")
        if ".." in parts:
            return f"{tool_name} {argument_name} cannot contain parent-directory traversal"
        return None

    def _edit(self, arguments: dict[str, Any], context: ExecutionContext) -> PolicyDecision:
        path = self._path_argument("edit_file", arguments, "file_path", context)
        if isinstance(path, PolicyDecision):
            return path
        if not path.is_file():
            return self._deny(ToolEffect.WRITE, "edit_file may only modify an existing Workspace file")
        if not self._is_in_plan(path, context.modify_files, context):
            return self._deny(ToolEffect.WRITE, "Path is not in the approved Plan modify_files list")
        normalized = dict(arguments)
        normalized["file_path"] = str(path)
        return self._allow(ToolEffect.WRITE, normalized, "Path is in the approved Plan modify_files list")

    def _write(self, arguments: dict[str, Any], context: ExecutionContext) -> PolicyDecision:
        path = self._path_argument("write_file", arguments, "file_path", context)
        if isinstance(path, PolicyDecision):
            return path
        in_modify_files = self._is_in_plan(path, context.modify_files, context)
        in_expected_files = self._is_in_plan(path, context.expected_files, context)
        if not in_modify_files and not in_expected_files:
            return self._deny(ToolEffect.WRITE, "Path is not in the approved Plan write scope")
        if in_expected_files and not in_modify_files and path.exists():
            return self._deny(ToolEffect.WRITE, "expected_files may only be created, not overwritten")
        normalized = dict(arguments)
        normalized["file_path"] = str(path)
        return self._allow(ToolEffect.WRITE, normalized, "Path is in the approved Plan write scope")

    def _validation_command(self, arguments: dict[str, Any], context: ExecutionContext) -> PolicyDecision:
        command = arguments.get("command")
        if not isinstance(command, str):
            return self._deny(ToolEffect.EXECUTE, "bash requires a string command")
        allowed_commands = [tuple(item) for item in context.validation_commands]
        matches = [item for item in allowed_commands if command == subprocess.list2cmdline(list(item))]
        if len(matches) != 1:
            return self._deny(ToolEffect.EXECUTE, "Command is not an exact approved Plan validation command")
        normalized = dict(arguments)
        normalized["command"] = command
        return PolicyDecision(
            PermissionAction.ALLOW,
            "Command exactly matches an approved Plan validation command",
            ToolEffect.EXECUTE,
            normalized,
            validation_command=matches[0],
        )

    def _path_argument(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        argument_name: str,
        context: ExecutionContext,
        *,
        default: str | None = None,
    ) -> Path | PolicyDecision:
        value = arguments.get(argument_name, default)
        if not isinstance(value, str) or not value:
            return self._deny(self._EFFECTS[tool_name], f"{tool_name} requires a non-empty {argument_name}")
        try:
            return context.resolve_workspace_path(value)
        except ValueError as error:
            return self._deny(self._EFFECTS[tool_name], str(error))

    @staticmethod
    def _is_in_plan(path: Path, allowed_paths: list[str], context: ExecutionContext) -> bool:
        try:
            target = context.relative_path(path)
        except ValueError:
            return False
        normalized_allowed = set()
        for item in allowed_paths:
            try:
                normalized_allowed.add(context.relative_path(context.resolve_workspace_path(item)))
            except ValueError:
                continue
        return target in normalized_allowed
