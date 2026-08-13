"""Structured validation command execution for FeaturePilot Workspaces."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


class ValidationCommandRunner:
    """Run one already-approved argument vector without invoking a shell."""

    def __init__(self, timeout_seconds: int = 120) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, command: Sequence[str], workspace_path: Path) -> str:
        """Run an exact command list in the Workspace and return a tool-style result."""

        if not command or any(not isinstance(item, str) or not item for item in command):
            return "Error: validation command must be a non-empty list of strings"
        try:
            completed = subprocess.run(
                list(command),
                cwd=workspace_path,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"Error: validation command timed out after {self.timeout_seconds}s"
        except OSError as error:
            return f"Error running validation command: {error}"

        output = completed.stdout
        if completed.stderr:
            output += f"\n[stderr]\n{completed.stderr}"
        if completed.returncode:
            output += f"\n[exit code: {completed.returncode}]"
        return output.strip() or "(no output)"
