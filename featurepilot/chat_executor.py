"""Repository-scoped, permission-gated Tool execution for Chat."""

from __future__ import annotations

import threading
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from corecoder.permissions import (
    DenyPermissionPrompt,
    PermissionAction,
    PermissionEffect,
    PermissionManager,
    PermissionRequest,
)
from corecoder.tools.base import Tool
from corecoder.tools.bash import BashTool
from corecoder.tools.edit import _changed_files
from corecoder.trusted_diff import FileWriteProposal, SourceSnapshotChanged, TrustedDiffError

from .permissions import ChatPermissionPolicy, command_effect, command_prefix, command_tokens

_PATH_ARGUMENTS = {
    "read_file": "file_path",
    "edit_file": "file_path",
    "write_file": "file_path",
    "glob": "path",
    "grep": "path",
}
_TOOL_EFFECTS = {
    "read_file": PermissionEffect.READ,
    "glob": PermissionEffect.READ,
    "grep": PermissionEffect.READ,
    "now": PermissionEffect.READ,
    "edit_file": PermissionEffect.WRITE,
    "write_file": PermissionEffect.WRITE,
    "bash": PermissionEffect.EXECUTE,
    "fetch_url": PermissionEffect.NETWORK,
    "agent": PermissionEffect.DELEGATE,
}


class RepositoryToolExecutor:
    """Bind tools to a repository and enforce C3 permissions before effects."""

    def __init__(
        self,
        repository_root: str | Path,
        permission_manager: PermissionManager | None = None,
    ):
        self.repository_root = Path(repository_root).resolve()
        self.permission_manager = permission_manager or PermissionManager(
            ChatPermissionPolicy(),
            DenyPermissionPrompt(),
        )
        self._side_effect_lock = threading.Lock()
        self._turn_stop_lock = threading.Lock()
        self._turn_stop_message: str | None = None

    def begin_turn(self) -> None:
        """Clear the optional stop request left by the previous Chat turn."""

        with self._turn_stop_lock:
            self._turn_stop_message = None

    def consume_turn_stop_message(self) -> str | None:
        """Return and clear a user-denial request to end the current turn."""

        with self._turn_stop_lock:
            message = self._turn_stop_message
            self._turn_stop_message = None
            return message

    def execute(self, tool: Tool, arguments: dict[str, Any]) -> str:
        return self.execute_call(tool, arguments, tool_call_id=uuid4().hex)

    def execute_call(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        tool_call_id: str,
    ) -> str:
        """Execute the exact Runtime-held call after path and permission checks."""

        normalized = dict(arguments)
        if tool.name == "glob":
            pattern_error = _validate_relative_pattern(normalized.get("pattern"), "glob pattern")
            if pattern_error:
                return f"Policy denied glob: {pattern_error}"
        if tool.name == "grep" and normalized.get("include") is not None:
            pattern_error = _validate_relative_pattern(normalized.get("include"), "grep include")
            if pattern_error:
                return f"Policy denied grep: {pattern_error}"
        path_argument = _PATH_ARGUMENTS.get(tool.name)
        if path_argument:
            value = normalized.get(path_argument, ".")
            try:
                normalized[path_argument] = str(self._resolve_path(value))
            except ValueError as error:
                return f"Policy denied {tool.name}: {error}"

        if tool.name in {"edit_file", "write_file"}:
            with self._side_effect_lock:
                return self._execute_write(tool, normalized, tool_call_id)
        if tool.name == "bash":
            if not isinstance(tool, BashTool):
                return "Error: bash tool does not support a repository working directory"
            with self._side_effect_lock:
                return self._execute_command(tool, normalized, tool_call_id)

        request = PermissionRequest(
            tool_call_id=tool_call_id,
            tool_name=tool.name,
            effect=_TOOL_EFFECTS.get(tool.name, PermissionEffect.UNKNOWN),
            normalized_arguments=normalized,
            reason="Repository-scoped read request",
            scope=str(self.repository_root),
        )
        decision = self.permission_manager.authorize(request)
        if decision.action is not PermissionAction.ALLOW:
            return _denied(tool.name, decision.reason)
        return tool.execute(**normalized)

    def _execute_command(
        self,
        tool: BashTool,
        normalized: dict[str, Any],
        tool_call_id: str,
    ) -> str:
        command = normalized.get("command")
        if not isinstance(command, str):
            return "Policy denied bash: command must be a string"
        tokens = command_tokens(command)
        request = PermissionRequest(
            tool_call_id=tool_call_id,
            tool_name="bash",
            effect=command_effect(command),
            normalized_arguments=normalized,
            reason="Shell command may have repository or environment side effects",
            scope=command,
            command_tokens=tokens,
            command_prefix=command_prefix(tokens),
        )
        decision = self.permission_manager.authorize(request)
        if decision.action is not PermissionAction.ALLOW:
            self._stop_after_interactive_denial("bash", decision)
            return _denied("bash", decision.reason)
        return tool.execute_in(
            command,
            cwd=str(self.repository_root),
            timeout=normalized.get("timeout", 120),
        )

    def _execute_write(
        self,
        tool: Tool,
        normalized: dict[str, Any],
        tool_call_id: str,
    ) -> str:
        force_prompt = False
        for _attempt in range(4):
            try:
                proposal = self._build_proposal(tool.name, normalized)
            except (OSError, TrustedDiffError) as error:
                return f"Error: {error}"
            if not proposal.has_changes:
                return f"No changes needed for {proposal.display_path}"

            request = PermissionRequest(
                tool_call_id=tool_call_id,
                tool_name=tool.name,
                effect=PermissionEffect.WRITE,
                normalized_arguments=normalized,
                reason=(
                    "Source changed after an earlier approval; review the regenerated diff"
                    if force_prompt
                    else "File content will change only after approval"
                ),
                scope=proposal.display_path,
                trusted_preview=proposal.trusted_diff,
                source_snapshot=proposal.source_snapshot,
            )
            decision = self.permission_manager.authorize(request, force_prompt=force_prompt)
            if decision.action is not PermissionAction.ALLOW:
                self._stop_after_interactive_denial(tool.name, decision)
                return _denied(tool.name, decision.reason)
            try:
                proposal.apply()
            except SourceSnapshotChanged:
                force_prompt = True
                continue
            except OSError as error:
                return f"Error: {error}"

            _changed_files.add(str(proposal.path))
            if tool.name == "edit_file":
                return f"Edited {proposal.display_path}\n{proposal.trusted_diff}"
            line_count = proposal.candidate_content.count("\n") + (
                1
                if proposal.candidate_content and not proposal.candidate_content.endswith("\n")
                else 0
            )
            return f"Wrote {line_count} lines to {proposal.display_path}\n{proposal.trusted_diff}"
        return "Error: source kept changing during approval; no file was written"

    def _build_proposal(self, tool_name: str, normalized: dict[str, Any]) -> FileWriteProposal:
        path = Path(normalized["file_path"])
        display_path = path.relative_to(self.repository_root).as_posix()
        if tool_name == "edit_file":
            old_string = normalized.get("old_string")
            new_string = normalized.get("new_string")
            if not isinstance(old_string, str) or not isinstance(new_string, str):
                raise TrustedDiffError("edit_file requires string old_string and new_string")
            return FileWriteProposal.for_edit(
                path,
                display_path=display_path,
                old_string=old_string,
                new_string=new_string,
            )
        content = normalized.get("content")
        if not isinstance(content, str):
            raise TrustedDiffError("write_file requires string content")
        return FileWriteProposal.for_write(path, display_path=display_path, content=content)

    def _resolve_path(self, value: object) -> Path:
        if not isinstance(value, (str, Path)) or not str(value):
            raise ValueError("path must be a non-empty string")
        raw = Path(str(value)).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (self.repository_root / raw).resolve()
        try:
            candidate.relative_to(self.repository_root)
        except ValueError as error:
            raise ValueError(f"path is outside repository: {value}") from error
        return candidate

    def _stop_after_interactive_denial(self, tool_name: str, decision) -> None:
        """Stop retry loops only when a user explicitly rejected an ASK request."""

        if decision.action is not PermissionAction.DENY or not decision.prompted:
            return
        with self._turn_stop_lock:
            self._turn_stop_message = (
                f"已按你的拒绝停止本轮后续操作；{tool_name} 没有执行。"
                "你可以继续说明下一步需求。"
            )


def _denied(tool_name: str, reason: str) -> str:
    return f"Permission denied {tool_name}: {reason}"


def _validate_relative_pattern(value: object, label: str) -> str | None:
    if not isinstance(value, str) or not value:
        return f"{label} must be a non-empty string"
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        return f"{label} must be relative to the repository search path"
    if ".." in value.replace("\\", "/").split("/"):
        return f"{label} cannot contain parent-directory traversal"
    return None
