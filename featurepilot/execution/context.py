"""The approved scope for one FeaturePilot execution attempt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..domain import PlanRecord, Run
from ..workspace import Workspace


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Connect one approved Plan, its Run record, and its isolated Workspace."""

    record: PlanRecord
    run: Run
    workspace: Workspace

    def __post_init__(self) -> None:
        if self.record.status != "approved":
            raise ValueError("ExecutionContext requires an approved PlanRecord")
        if self.run.plan_id != self.record.id:
            raise ValueError("Run plan id must match the PlanRecord used for execution")
        if Path(self.run.workspace_path or "").resolve() != self.workspace.path.resolve():
            raise ValueError("Run workspace path must match the supplied Workspace")

    @property
    def plan(self):
        """Convenience access to the approved structured Plan."""

        return self.record.plan

    def resolve_workspace_path(self, value: str) -> Path:
        """Resolve a relative or in-workspace absolute path without allowing escapes."""

        raw_path = Path(value).expanduser()
        workspace_root = self.workspace.path.resolve()
        candidate = raw_path.resolve() if raw_path.is_absolute() else (workspace_root / raw_path).resolve()
        try:
            candidate.relative_to(workspace_root)
        except ValueError as error:
            raise ValueError(f"Path is outside the Workspace: {value}") from error
        return candidate

    def relative_path(self, path: Path) -> str:
        """Return one normalized Workspace-relative path for Plan scope comparison."""

        return path.resolve().relative_to(self.workspace.path.resolve()).as_posix()
