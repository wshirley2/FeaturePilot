"""Workspace-bound search implementations used by controlled TechPilot runs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .context import ExecutionContext

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build"}


class WorkspaceSearchRunner:
    """Run glob and grep without reading through links that leave the Workspace."""

    def __init__(self, context: ExecutionContext) -> None:
        self.context = context
        self._workspace_root = context.workspace.path.resolve()

    def glob(self, arguments: dict[str, Any]) -> str:
        base = Path(str(arguments["path"])).resolve()
        pattern = str(arguments["pattern"])
        try:
            hits = [path for path in base.glob(pattern) if self._is_safe_path(path)]
            hits.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        except (OSError, ValueError) as error:
            return f"Error: {error}"

        total = len(hits)
        result = "\n".join(str(path) for path in hits[:100])
        if total > 100:
            result += f"\n... ({total} matches, showing first 100)"
        return result or "No files matched."

    def grep(self, arguments: dict[str, Any]) -> str:
        try:
            regex = re.compile(str(arguments["pattern"]))
        except re.error as error:
            return f"Invalid regex: {error}"

        base = Path(str(arguments["path"])).resolve()
        include = arguments.get("include")
        if not base.exists():
            return f"Error: {base} not found"
        files = [base] if base.is_file() else self._walk(base, str(include) if include is not None else None)

        matches: list[str] = []
        for path in files:
            if not self._is_safe_file(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{path}:{line_number}: {line.rstrip()}")
                    if len(matches) >= 200:
                        matches.append("... (200 match limit reached)")
                        return "\n".join(matches)
        return "\n".join(matches) if matches else "No matches found."

    def _walk(self, root: Path, include: str | None) -> list[Path]:
        results: list[Path] = []
        try:
            candidates = root.rglob(include or "*")
            for path in candidates:
                try:
                    relative_parts = path.relative_to(root).parts
                except ValueError:
                    continue
                if any(part in _SKIP_DIRS for part in relative_parts):
                    continue
                if self._is_safe_file(path):
                    results.append(path)
                if len(results) >= 5000:
                    break
        except (OSError, ValueError):
            return results
        return results

    def _is_safe_file(self, path: Path) -> bool:
        return path.is_file() and self._is_safe_path(path)

    def _is_safe_path(self, path: Path) -> bool:
        """Reject broken links and links whose resolved target leaves the Workspace."""

        try:
            path.resolve(strict=True).relative_to(self._workspace_root)
        except (OSError, ValueError):
            return False
        return True
