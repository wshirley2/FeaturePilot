"""Bridge FeaturePilot's Tool protocol to its Plan-aware policy."""

from __future__ import annotations

import threading
from typing import Any

from featurepilot.engine.tools import get_tool
from featurepilot.engine.tools.base import Tool

from .context import ExecutionContext
from .policy import ToolEffect, ToolPolicy
from .search import WorkspaceSearchRunner
from .validation import ValidationCommandRunner

_FEATUREPILOT_TOOL_NAMES = ("read_file", "glob", "grep", "edit_file", "write_file", "bash", "now")


def build_featurepilot_tools() -> list[Tool]:
    """Return the deliberately small Tool set allowed for a FeaturePilot Run."""

    tools: list[Tool] = []
    for name in _FEATUREPILOT_TOOL_NAMES:
        tool = get_tool(name)
        if tool is None:
            raise RuntimeError(f"FeaturePilot tool registry is missing required tool {name!r}")
        tools.append(tool)
    return tools


class WorkspaceToolExecutor:
    """Apply policy before running a Tool inside one Workspace."""

    def __init__(
        self,
        context: ExecutionContext,
        policy: ToolPolicy | None = None,
        validation_runner: ValidationCommandRunner | None = None,
    ) -> None:
        self.context = context
        self.policy = policy or ToolPolicy()
        self.validation_runner = validation_runner or ValidationCommandRunner()
        self.search_runner = WorkspaceSearchRunner(context)
        self._side_effect_lock = threading.Lock()

    def execute(self, tool: Tool, arguments: dict[str, Any]) -> str:
        """Return a policy denial or run an allowed request with normalized arguments."""

        decision = self.policy.decide(tool.name, arguments, self.context)
        if not decision.allowed:
            return f"Policy denied {tool.name}: {decision.reason}"
        if decision.effect in {ToolEffect.WRITE, ToolEffect.EXECUTE}:
            with self._side_effect_lock:
                return self._execute_allowed(tool, decision.arguments, decision.validation_command)
        return self._execute_allowed(tool, decision.arguments, decision.validation_command)

    def _execute_allowed(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        validation_command: tuple[str, ...] | None,
    ) -> str:
        if tool.name == "bash":
            if validation_command is None:
                return "Policy denied bash: validation command metadata is missing"
            return self.validation_runner.run(validation_command, self.context.workspace.path)
        if tool.name == "glob":
            return self.search_runner.glob(arguments)
        if tool.name == "grep":
            return self.search_runner.grep(arguments)
        return tool.execute(**arguments)
