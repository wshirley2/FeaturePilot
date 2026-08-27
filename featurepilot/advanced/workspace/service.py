"""Application service for creating workspaces from approved Plans."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ...domain import ExecutionScope, PlanRecord, Run
from ...runtime.contracts import TaskRuntimePaths
from .backend import Workspace, WorkspaceBackend


class WorkspaceService:
    """Bind one approved PlanRecord to a new isolated Run workspace."""

    def __init__(self, backend: WorkspaceBackend) -> None:
        self.backend = backend

    def create_for_plan(self, record: PlanRecord) -> tuple[Run, Workspace]:
        """Compatibility entry point for the existing approved-Plan path."""

        if record.status != "approved":
            raise ValueError("Only approved plans can create a Workspace")
        return self.create_for_scope(ExecutionScope.from_plan(record))

    def create_for_scope(self, scope: ExecutionScope) -> tuple[Run, Workspace]:
        """Create an isolated Workspace for an already approved execution scope."""

        run = Run(task_id=scope.task.id, plan_id=scope.plan_id)
        workspace = self.backend.create(
            scope.source_repository,
            run.id,
            label=_workspace_label(scope.reference),
        )
        run.workspace_path = str(workspace.path)
        run.source_snapshot = workspace.source_snapshot
        self.save_run(run)
        return run, workspace

    def save_run(
        self,
        run: Run,
        runtime_paths: TaskRuntimePaths | None = None,
    ) -> Path:
        """Atomically persist the current Run state beside its Workspace."""

        if not run.workspace_path:
            raise ValueError("Run must have a workspace path before it can be saved")
        workspace_path = Path(run.workspace_path).resolve()
        if workspace_path.name != "workspace" or not workspace_path.is_dir():
            raise ValueError(f"Run workspace directory does not exist: {run.workspace_path}")

        metadata_path = (
            runtime_paths.run_metadata_path
            if runtime_paths is not None
            else workspace_path.parent / "run.json"
        )
        if metadata_path.parent != workspace_path.parent:
            raise ValueError("Run metadata must remain beside its Workspace")
        temporary_path = workspace_path.parent / f".run-{run.id}.tmp"
        payload = f"{json.dumps(run.to_dict(), ensure_ascii=False, indent=2)}\n"
        try:
            temporary_path.write_text(payload, encoding="utf-8")
            temporary_path.replace(metadata_path)
        except OSError:
            temporary_path.unlink(missing_ok=True)
            raise
        return metadata_path


def _workspace_label(reference: str) -> str:
    """Turn a human-readable scope reference into a safe directory label.

    Tool Call IDs are provider-owned and may include underscores or other
    punctuation that the Workspace backend deliberately rejects. The original
    reference remains in Run Events; only the directory label is normalized.
    """

    label = re.sub(r"[^A-Za-z0-9.-]+", "-", reference).strip(".-")
    return label or "execution"
