"""Contracts and value objects for isolated workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Workspace:
    """A disposable copy of a repository associated with one Run."""

    run_id: str
    source_path: Path
    path: Path
    source_snapshot: str

    @property
    def display_id(self) -> str:
        """Short Run identifier used by people and directory names."""

        return self.run_id[:8]

    def resolve_path(self, relative_path: Path) -> Path:
        """Resolve one workspace-relative path without allowing escapes."""

        if relative_path.is_absolute():
            raise ValueError("Workspace paths must be relative")
        workspace_root = self.path.resolve()
        candidate = (workspace_root / relative_path).resolve()
        try:
            candidate.relative_to(workspace_root)
        except ValueError as error:
            raise ValueError(f"Workspace path escapes its root: {relative_path}") from error
        return candidate


class WorkspaceBackend(Protocol):
    """Creates isolated workspaces for approved implementation plans."""

    def create(self, source_path: Path, run_id: str, label: str | None = None) -> Workspace:
        """Create and return one isolated repository copy."""
