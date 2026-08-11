"""Application service for creating workspaces from approved Plans."""

from __future__ import annotations

import json
from pathlib import Path

from ..domain import PlanRecord, Run
from .backend import Workspace, WorkspaceBackend


class WorkspaceService:
    """Bind one approved PlanRecord to a new isolated Run workspace."""

    def __init__(self, backend: WorkspaceBackend) -> None:
        self.backend = backend

    def create_for_plan(self, record: PlanRecord) -> tuple[Run, Workspace]:
        if record.status != "approved":
            raise ValueError(f"Only approved plans can create a workspace; current status is {record.status!r}")
        run = Run(task_id=record.plan.task_id, plan_id=record.id)
        workspace = self.backend.create(Path(record.repository), run.id, label=record.reference)
        run.workspace_path = str(workspace.path)
        run.source_snapshot = workspace.source_snapshot
        metadata_path = workspace.path.parent / "run.json"
        metadata_path.write_text(
            f"{json.dumps(run.to_dict(), ensure_ascii=False, indent=2)}\n",
            encoding="utf-8",
        )
        return run, workspace
