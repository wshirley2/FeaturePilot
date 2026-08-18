"""Repository-scoped Tool execution used by the initial Chat runtime."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from corecoder.tools.base import Tool
from corecoder.tools.bash import BashTool

_PATH_ARGUMENTS = {
    "read_file": "file_path",
    "edit_file": "file_path",
    "write_file": "file_path",
    "glob": "path",
    "grep": "path",
}


class RepositoryToolExecutor:
    """Bind relative file and process operations to one repository root.

    This is a C2 file-path and starting-directory boundary, not a command
    sandbox or the complete C3 permission system. Bash still uses the existing
    safety behavior and may reference paths outside the repository.
    """

    def __init__(self, repository_root: str | Path):
        self.repository_root = Path(repository_root).resolve()

    def execute(self, tool: Tool, arguments: dict[str, Any]) -> str:
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

        if tool.name == "bash":
            if not isinstance(tool, BashTool):
                return "Error: bash tool does not support a repository working directory"
            return tool.execute_in(
                normalized["command"],
                cwd=str(self.repository_root),
                timeout=normalized.get("timeout", 120),
            )
        return tool.execute(**normalized)

    def _resolve_path(self, value: object) -> Path:
        raw = Path(str(value)).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (self.repository_root / raw).resolve()
        try:
            candidate.relative_to(self.repository_root)
        except ValueError as error:
            raise ValueError(f"path is outside repository: {value}") from error
        return candidate


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
