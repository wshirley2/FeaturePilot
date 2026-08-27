"""The approved scope for one TechPilot execution attempt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..advanced.workspace import Workspace
from ..domain import ExecutionScope, PlanRecord, Run


@dataclass(frozen=True, slots=True, init=False)
class ExecutionContext:
    """Connect one approved execution scope, its Run record, and Workspace."""

    scope: ExecutionScope
    run: Run
    workspace: Workspace

    def __init__(
        self,
        *,
        scope: ExecutionScope | None = None,
        record: PlanRecord | None = None,
        run: Run,
        workspace: Workspace,
    ) -> None:
        """Accept a PlanRecord for legacy callers and adapt it at the boundary."""

        if (scope is None) == (record is None):
            raise ValueError("Pass exactly one of scope or record to ExecutionContext")
        resolved_scope = scope or ExecutionScope.from_plan(record)
        if resolved_scope.run_id is None:
            resolved_scope = resolved_scope.with_execution(
                run_id=run.id,
                workspace_path=workspace.path,
            )
        object.__setattr__(self, "scope", resolved_scope)
        object.__setattr__(self, "run", run)
        object.__setattr__(self, "workspace", workspace)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.run.task_id != self.scope.task.id:
            raise ValueError("Run task id must match the ExecutionScope")
        if self.run.plan_id != self.scope.plan_id:
            raise ValueError("Run plan id must match the ExecutionScope")
        if self.scope.run_id != self.run.id:
            raise ValueError("ExecutionScope run id must match the supplied Run")
        if self.scope.workspace_path is None or self.scope.workspace_path.resolve() != self.workspace.path.resolve():
            raise ValueError("ExecutionScope workspace path must match the supplied Workspace")
        if Path(self.run.workspace_path or "").resolve() != self.workspace.path.resolve():
            raise ValueError("Run workspace path must match the supplied Workspace")

    @property
    def modify_files(self) -> tuple[str, ...]:
        return self.scope.modify_files

    @property
    def expected_files(self) -> tuple[str, ...]:
        return self.scope.expected_files

    @property
    def validation_commands(self) -> tuple[tuple[str, ...], ...]:
        return self.scope.validation_commands

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
